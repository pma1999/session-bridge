"""Windows-side WSL transport. All subprocess calls use fixed argv arrays."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from .errors import BridgeError
from .models import Profile, WriteRequest

_DISTRO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_OPS = {"discover", "read", "capability", "write", "verify", "inspect", "undo", "resume_argv"}
MAX_REQUEST_BYTES = 64 * 1024 * 1024


def _wsl_exe() -> str:
    exe = shutil.which("wsl.exe") or shutil.which("wsl")
    if not exe:
        raise BridgeError("wsl_unavailable", "No se encontró WSL en este equipo.")
    return exe


def _decode_output(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:8]:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")


def status() -> dict[str, Any]:
    """List registered distributions without starting stopped ones."""
    try:
        result = subprocess.run(
            [_wsl_exe(), "--list", "--verbose"], capture_output=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BridgeError("wsl_status_failed", "No se pudo consultar el estado de WSL.") from None
    if result.returncode != 0:
        raise BridgeError(
            "wsl_status_failed",
            "WSL no pudo devolver el estado de las distribuciones.",
            exit_code=result.returncode,
        )
    lines = _decode_output(result.stdout).replace("\r", "").splitlines()
    distros: list[dict[str, Any]] = []
    for line in lines:
        cleaned = line.strip()
        if cleaned.startswith("*"):
            cleaned = cleaned[1:].strip()
        match = re.match(
            r"^(.*?)\s+(Running|Stopped|En ejecución|Detenido)\s+(\d+)\s*$", cleaned, re.IGNORECASE
        )
        if match:
            name, state, version = match.groups()
            if name.casefold() in {"docker-desktop", "docker-desktop-data"}:
                continue
            distros.append(
                {"name": name, "running": state.casefold() in {"running", "en ejecución"}, "version": version}
            )
    return {"available": True, "distributions": distros}


def _valid_distro(distro: str) -> None:
    if not _DISTRO.fullmatch(distro):
        raise BridgeError("invalid_distro", "El nombre de distribución WSL no es válido.")


def call(profile: Profile, op: str, data: dict[str, Any]) -> Any:
    """Send one allowlisted version-1 JSON request to the installed Linux helper."""
    if profile.environment != "wsl" or not profile.distro:
        raise BridgeError(
            "invalid_wsl_profile", "La operación requiere un perfil WSL con distribución explícita."
        )
    if not profile.helper or not PurePosixPath(profile.helper).is_absolute() or "\x00" in profile.helper:
        raise BridgeError(
            "invalid_wsl_profile", "El perfil WSL necesita la ruta Linux absoluta del Python ayudante."
        )
    _valid_distro(profile.distro)
    if op not in _OPS:
        raise BridgeError("unsupported_helper_operation", "La operación no está permitida para el helper.")
    state = next(
        (item for item in status()["distributions"] if item["name"].casefold() == profile.distro.casefold()),
        None,
    )
    if state is None:
        raise BridgeError(
            "wsl_distro_missing",
            "La distribución configurada no está registrada en WSL.",
            distribution=profile.distro,
        )
    if not state["running"]:
        raise BridgeError(
            "wsl_distro_stopped",
            "La distribución está detenida. Iníciala explícitamente con `wsl start`.",
            distribution=profile.distro,
        )
    args = dict(data)
    if op == "write":
        try:
            request = WriteRequest.model_validate(args.get("request"))
        except (ValidationError, TypeError, ValueError, AttributeError):
            raise BridgeError(
                "invalid_helper_arguments", "La solicitud de escritura no cumple el protocolo v1."
            ) from None
        if request.profile.model_dump(mode="json") != profile.model_dump(mode="json"):
            raise BridgeError("profile_mismatch", "La escritura no corresponde al perfil WSL seleccionado.")
        args["request"] = request.model_dump(mode="json")
    else:
        args["profile"] = profile.model_dump(mode="json")
    request = json.dumps({"protocol_version": 1, "op": op, "args": args}, ensure_ascii=False).encode("utf-8")
    if len(request) > MAX_REQUEST_BYTES:
        raise BridgeError(
            "request_too_large",
            "La solicitud WSL supera el límite explícito de 64 MiB.",
            limit_bytes=MAX_REQUEST_BYTES,
        )
    try:
        result = subprocess.run(
            [
                _wsl_exe(),
                "--distribution",
                profile.distro,
                "--exec",
                profile.helper,
                "-m",
                "session_bridge.helper",
            ],
            input=request,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BridgeError("wsl_helper_failed", "No se pudo comunicar con el helper de WSL.") from None
    if result.returncode != 0:
        raise BridgeError(
            "wsl_helper_failed", "El helper de WSL rechazó la operación.", exit_code=result.returncode
        )
    try:
        response = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BridgeError(
            "wsl_protocol_error", "El helper de WSL devolvió una respuesta no válida."
        ) from None
    if not isinstance(response, dict) or response.get("protocol_version") != 1 or "ok" not in response:
        raise BridgeError("wsl_protocol_error", "El helper de WSL devolvió una respuesta no válida.")
    if response["ok"] is not True:
        error = response.get("error") or {}
        raise BridgeError(
            str(error.get("code", "wsl_helper_error")),
            str(error.get("message", "La operación WSL falló.")),
            **(error.get("details") if isinstance(error.get("details"), dict) else {}),
        )
    return response.get("data")


def start(distro: str) -> dict[str, Any]:
    """Start one explicitly selected registered distribution."""
    _valid_distro(distro)
    state = next(
        (item for item in status()["distributions"] if item["name"].casefold() == distro.casefold()), None
    )
    if state is None:
        raise BridgeError(
            "wsl_distro_missing", "La distribución indicada no está registrada en WSL.", distribution=distro
        )
    if not state["running"]:
        try:
            result = subprocess.run(
                [_wsl_exe(), "--distribution", distro, "--exec", "true"],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise BridgeError("wsl_start_failed", "No se pudo iniciar la distribución elegida.") from None
        if result.returncode != 0:
            raise BridgeError(
                "wsl_start_failed", "No se pudo iniciar la distribución elegida.", exit_code=result.returncode
            )
    return {"distribution": distro, "running": True}


def destination_path(profile: Profile, path: Path) -> str:
    """Map an owned Windows context file without interpolating paths or starting a stopped distro."""
    _valid_distro(profile.distro or "")
    state = next((d for d in status()["distributions"] if d["name"] == profile.distro), None)
    if not state or not state["running"]:
        raise BridgeError(
            "wsl_distro_stopped", "Inicia explícitamente la distribución antes de preparar su traspaso."
        )
    try:
        result = subprocess.run(
            [_wsl_exe(), "--distribution", profile.distro, "--exec", "wslpath", "-a", "-u", str(path)],
            capture_output=True,
            check=False,
            timeout=10,
        )
        mapped = result.stdout.decode("utf-8").strip()
        if result.returncode or not mapped.startswith("/") or "\x00" in mapped or "\n" in mapped:
            raise ValueError("invalid mapping")
        return mapped
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise BridgeError("wsl_path_mapping", "No se pudo convertir la ruta del contexto para WSL.") from None


def install(distro: str, wheel_path: Path) -> dict[str, Any]:
    """Explicitly install the supplied wheel into a dedicated rootless WSL venv."""
    _valid_distro(distro)
    wheel = Path(wheel_path).resolve(strict=True)
    if not wheel.is_file() or wheel.suffix.casefold() != ".whl":
        raise BridgeError("invalid_wheel", "Indica el archivo wheel de Session Bridge.")
    if not wheel.name.casefold().startswith(("session_bridge-", "session-bridge-")):
        raise BridgeError("invalid_wheel", "El wheel debe pertenecer a Session Bridge.")
    # WSL can address a Windows drive through /mnt/<drive>; pass the path as one argv token.
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", str(wheel))
    if match:
        linux_wheel = f"/mnt/{match.group(1).lower()}/{match.group(2).replace(chr(92), '/')}"
    else:
        raise BridgeError(
            "wheel_path_unsupported", "El wheel debe estar en una unidad Windows montada en WSL."
        )
    exe = _wsl_exe()
    # The fixed Python bootstrap computes its own home directory. The wheel stays one argv token.
    bootstrap = "\n".join(
        [
            "import json,sys",
            "sys.path.insert(0,sys.argv[1])",
            "from session_bridge.bootstrap import install_wsl",
            "from session_bridge.errors import BridgeError",
            "try:",
            "    result=install_wsl(sys.argv[1],sys.argv[2])",
            "except BridgeError as exc:",
            "    print(json.dumps({'protocol_version':1,'ok':False,'error':exc.as_dict()}))",
            "    sys.exit(2)",
            "print(json.dumps(result,separators=(',',':')))",
        ]
    )
    try:
        result = subprocess.run(
            [exe, "--distribution", distro, "--exec", "python3", "-c", bootstrap, linux_wheel, distro],
            capture_output=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BridgeError(
            "wsl_install_failed", "No se pudo preparar el entorno de Session Bridge en WSL."
        ) from None
    if result.returncode != 0:
        try:
            packet = json.loads(_decode_output(result.stdout))
            error = packet.get("error", {}) if packet.get("protocol_version") == 1 else {}
            stage = error.get("details", {}).get("stage")
            if (
                error.get("code") == "bootstrap_failed"
                and isinstance(stage, str)
                and re.fullmatch(r"[a-z_]{1,64}", stage)
            ):
                raise BridgeError(
                    "bootstrap_failed",
                    "No se pudo preparar el entorno de Session Bridge en WSL.",
                    stage=stage,
                )
        except (ValueError, TypeError, AttributeError):
            pass
        raise BridgeError(
            "wsl_install_failed",
            "No se pudo preparar el entorno de Session Bridge en WSL.",
            exit_code=result.returncode,
        )
    try:
        metadata = json.loads(_decode_output(result.stdout))
    except (json.JSONDecodeError, UnicodeError):
        raise BridgeError(
            "wsl_install_protocol_error", "La instalación no devolvió metadatos válidos."
        ) from None
    required_paths = ("helper", "home", "install_root", "launcher")
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("profiles"), list)
        or any(
            not isinstance(metadata.get(field), str) or not PurePosixPath(metadata[field]).is_absolute()
            for field in required_paths
        )
        or metadata.get("path_status") not in {"added", "managed", "conflict", "unavailable"}
        or not isinstance(metadata.get("on_path"), bool)
    ):
        raise BridgeError("wsl_install_protocol_error", "La instalación no devolvió metadatos válidos.")
    profiles = []
    try:
        for item in metadata["profiles"]:
            profile = Profile.model_validate(item)
            if (
                profile.environment != "wsl"
                or profile.distro != distro
                or profile.helper != metadata["helper"]
                or not PurePosixPath(profile.home).is_absolute()
            ):
                raise ValueError("profile metadata mismatch")
            profiles.append(profile.model_dump(mode="json"))
    except (ValidationError, TypeError, ValueError):
        raise BridgeError(
            "wsl_install_protocol_error", "La instalación no devolvió perfiles válidos."
        ) from None
    return {"installed": True, **metadata, "profiles": profiles}
