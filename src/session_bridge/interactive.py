"""Interactive terminal flow for Session Bridge."""

from __future__ import annotations

from typing import Any

from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.completion import FuzzyCompleter, WordCompleter

from .errors import BridgeError


def _visible(value: Any) -> str:
    text = str(value)
    return "".join(f"\\x{ord(ch):02x}" if ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F else ch for ch in text)


def confirm(message: str, default: bool = False) -> bool:
    suffix = " [S/n]: " if default else " [s/N]: "
    while True:
        answer = prompt(message + suffix).strip().casefold()
        if not answer:
            return default
        if answer in {"s", "si", "sí", "y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Responde s o n; Intro conserva la opción indicada.")


def _choose(label: str, values: list[str]) -> str:
    if not values:
        raise BridgeError("no_choices", f"No hay opciones disponibles para {label}.")
    session: PromptSession[str] = PromptSession(completer=FuzzyCompleter(WordCompleter(values)))
    while True:
        answer = session.prompt(f"{label} (escribe para buscar): ").strip()
        if answer in values:
            return answer
        matches = [value for value in values if answer.casefold() in value.casefold()]
        if len(matches) == 1:
            return matches[0]
        if not answer and len(values) == 1:
            return values[0]
        print("Elige una opción exacta de la lista.")
        for value in matches or values:
            print(f"  {_visible(value)}")


def prompt_profile(values: dict[str, Any]) -> dict[str, Any]:
    """Complete a profile from explicit terminal answers."""
    from prompt_toolkit import prompt

    for field, label in (("id", "Identificador"), ("tool", "Herramienta"), ("home", "Carpeta de datos")):
        if not values.get(field):
            values[field] = prompt(f"{label}: ").strip()
    if not values.get("tool"):
        raise BridgeError("missing_required_arguments", "Indica la herramienta del perfil.")
    if values["tool"] not in {"claude", "opencode", "codex"}:
        raise BridgeError("invalid_tool", "La herramienta debe ser claude, opencode o codex.")
    if values.get("environment") == "wsl" and not values.get("distro"):
        values["distro"] = prompt("Distribución WSL: ").strip()
    if values.get("environment") == "wsl" and not values.get("helper"):
        values["helper"] = prompt("Python del helper en Linux: ").strip()
    return values


def run_wizard(bridge: Any) -> dict[str, Any]:
    """Select a source and target, inspect a preview, then request explicit execution."""
    sessions = bridge.list_sessions()
    refs = {
        f"{_visible(ref.title or 'Sin título')} | {_visible(ref.cwd)} | {_visible(ref.key)}": ref
        for ref in sessions
    }
    choice = _choose("Sesión de origen", sorted(refs))
    ref = refs[choice]
    key = ref.key
    snapshot = ref.active is True
    if snapshot:
        snapshot = confirm("La sesión está activa. ¿Usar una instantánea de solo lectura?", default=False)
        if not snapshot:
            raise BridgeError("source_active", "La sesión activa requiere una instantánea explícita.")
    conversation = bridge.show(key, snapshot=snapshot)
    profiles = bridge.profiles()
    targets = sorted(f"{profile.id} ({profile.tool})" for profile in profiles if profile.enabled)
    target = _choose("Perfil de destino", targets)
    target_id = target.split(" (", 1)[0]
    context = _choose("Contexto", ["current", "all"])
    context_from = None
    if conversation.context_ambiguous and context != "all":
        event_ids = sorted(event.id for event in conversation.events if event.active)
        context_from = _choose("La rama es ambigua; selecciona el ID de inicio o all", ["all", *event_ids])
        if context_from == "all":
            context, context_from = "all", None
    mode = _choose("Modo", ["native", "handoff"])
    cwd = None
    while True:
        try:
            preview = bridge.preview(
                key,
                target_id,
                cwd=cwd,
                context=context,
                context_from=context_from,
                snapshot=snapshot,
                mode=mode,
            )
            break
        except BridgeError as exc:
            if exc.code not in {"cwd_missing", "cwd_required"}:
                raise
            cwd = prompt("Carpeta de trabajo en el destino: ").strip()
            if not cwd:
                return {"cancelled": True}
    if mode == "native" and preview.get("native_available") is False:
        print(_visible(preview.get("capability", {}).get("reason", "Compatibilidad nativa no verificada.")))
        if not confirm(
            "¿Preparar explícitamente un traspaso de contexto sin historial nativo?", default=False
        ):
            return {"cancelled": True, "preview": preview}
        mode = "handoff"
        preview = bridge.preview(
            key, target_id, cwd=cwd, context=context, context_from=context_from, snapshot=snapshot, mode=mode
        )
    print(f"Origen: {_visible(ref.title or key)}")
    print(f"Destino: {_visible(target)}; contexto: {context}; modo: {mode}")
    print(f"Carpeta de destino: {_visible(preview.get('cwd', cwd or conversation.source.cwd))}")
    print(
        f"Mensajes transferibles: {preview.get('message_count', 0)}; "
        f"eventos conservados: {preview.get('archived_event_count', 0)}; "
        f"adjuntos: {preview.get('attachments', 0)}"
    )
    fidelity = preview.get("fidelity", {})
    print(
        f"Adaptados: {len(fidelity.get('adapted', []))}; "
        f"solo en archivo: {len(fidelity.get('archive_only', []))}; "
        f"ausentes: {len(fidelity.get('missing', []))}"
    )
    print(_visible(preview.get("capability", {}).get("reason", "")))
    if "source_activity_unknown" in fidelity.get("warnings", []):
        print(
            "No se puede confirmar si la sesión de origen está activa. La captura detectará cambios mientras lee."
        )
    if not confirm("¿Continuar con la transferencia?", default=False):
        return {"cancelled": True, "preview": preview}
    keep_synced = False
    local_profiles = {profile.id: profile for profile in profiles}
    if mode == "native" and all(
        local_profiles[identifier].environment == "windows" for identifier in (ref.profile_id, target_id)
    ):
        keep_synced = confirm(
            "¿Mantener esta conversación sincronizada? Solo se seguirá su historial, sin archivos ni procesos.",
            default=False,
        )
    transfer = bridge.convert(
        key, target_id, cwd=cwd, context=context, context_from=context_from, snapshot=snapshot, mode=mode
    )
    if keep_synced:
        from .sync import SyncService
        from .sync_cli import _ensure_worker

        service = SyncService(bridge=bridge)
        name = (
            prompt("Nombre para continuarla desde cualquier herramienta (Intro usa su ID): ").strip() or None
        )
        linked = service.link(key, name=name, transfer_id=transfer.id)
        linked["worker"] = _ensure_worker(bridge.data_dir)
        return {"transfer": transfer, "sync": linked}
    return transfer


def run_home(bridge: Any) -> dict[str, Any]:
    """Keep linked conversations discoverable in the existing keyboard-first CLI."""
    from .sync import SyncService
    from .sync_cli import _ensure_worker

    service = SyncService(bridge=bridge)
    links = service.list()
    if not links:
        return run_wizard(bridge)
    labels = {f"{_visible(link['name'])} | {_visible(link['state'])} | {link['id']}": link for link in links}
    choice = _choose("Conversación o nueva conversión", ["Convertir otra sesión", *sorted(labels)])
    if choice == "Convertir otra sesión":
        return run_wizard(bridge)
    linked = labels[choice]
    if linked["paused"]:
        if not confirm("La conversación está en pausa. ¿Reanudar su seguimiento?", default=False):
            return {"cancelled": True}
        service.unpause(linked["id"])
    service.poll(linked["id"], force=True)
    linked = service.status(linked["id"])
    branch = None
    if linked["state"] in {"conflict", "waiting", "error"}:
        if not linked["heads"]:
            print("Aún no hay un turno terminado. El seguimiento continuará en segundo plano.")
            _ensure_worker(bridge.data_dir)
            return linked
        print("Hay ramas o un origen pendiente. Elige expresamente la instantánea que quieres abrir.")
        branches = {
            f"{head['id']} | {_visible(head['source_key'])} | {head['reason']}": head["id"]
            for head in linked["heads"]
        }
        branch = branches[_choose("Rama verificada", sorted(branches))]
    profiles = {
        f"{profile.id} ({profile.tool})": profile.id
        for profile in bridge.profiles()
        if profile.environment == "windows" and profile.enabled
    }
    target = profiles[_choose("Abrir con", sorted(profiles))]
    _ensure_worker(bridge.data_dir)
    cwd = None
    while True:
        try:
            return service.open(linked["id"], target, branch=branch, cwd=cwd, launch=True)
        except BridgeError as exc:
            if exc.code not in {"cwd_missing", "cwd_required"}:
                raise
            cwd = prompt("Carpeta de trabajo existente en Windows: ").strip()
            if not cwd:
                return {"cancelled": True}
