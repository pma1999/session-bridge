"""Interactive terminal flow for Session Bridge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.completion import Completer, Completion

from .catalog import same_directory, search_rank, search_text
from .errors import BridgeError

ALL_PROJECTS = "Buscar en todos los proyectos"
CURRENT_PROJECT = "Buscar solo en la carpeta actual"
CANCEL = "Cancelar"


class _ChoiceCompleter(Completer):
    def __init__(self, values: list[str]):
        self.values = [(value, search_text(value)) for value in values]

    def matches(self, query: str) -> list[str]:
        matches = [
            (value, rank)
            for value, key in self.values
            if (rank := search_rank(query, value, normalized_value=key)) is not None
        ]
        # Keep recent/catalogue order for ties; an exact phrase precedes scattered keywords.
        matches.sort(key=lambda item: item[1])
        return [value for value, _ in matches]

    def get_completions(self, document, complete_event):
        query = document.text
        for value in self.matches(query):
            # Completion cannot delete a suffix after the cursor; avoid inserting a broken choice.
            if document.text_after_cursor:
                continue
            yield Completion(value, start_position=-len(document.text_before_cursor), display=_visible(value))


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
    completer = _ChoiceCompleter(values)
    session: PromptSession[str] = PromptSession(completer=completer, complete_while_typing=True)
    while True:
        answer = session.prompt(f"{label} (escribe para buscar): ").strip()
        if answer in values:
            return answer
        matches = completer.matches(answer)
        if len(matches) == 1:
            return matches[0]
        if not answer and len(values) == 1:
            return values[0]
        print(
            "Hay varias coincidencias; concreta la búsqueda o usa Tab y las flechas."
            if matches
            else "Sin coincidencias. Prueba otras palabras; Ctrl+C cancela."
        )
        for value in (matches or values)[:30]:
            print(f"  {_visible(value)}")
        if len(matches or values) > 30:
            print("Mostrando 30 opciones. Escribe para filtrar; Tab permite recorrer las coincidencias.")


def _source_session(bridge: Any, cwd: str | None):
    launch_directory = str(Path.cwd())
    while True:
        print("Carpeta: " + _visible(cwd) if cwd is not None else "Ámbito: todos los proyectos")
        sessions = bridge.list_sessions(cwd=cwd)
        for warning in getattr(bridge, "discovery_errors", []):
            print("Aviso de catálogo: " + _visible(warning.get("message", "No se pudo leer un perfil.")))
        refs = {
            f"{_visible(ref.title or 'Sin título')} | {_visible(ref.cwd)} | {_visible(ref.key)}": ref
            for ref in sessions
        }
        scope = ALL_PROJECTS if cwd is not None else CURRENT_PROJECT
        if not refs:
            print("No hay sesiones en este ámbito. Puedes cambiar de ámbito o cancelar.")
        print("Escribe título, carpeta o ID. Tab muestra opciones; Ctrl+C cancela. " + scope + ".")
        choice = _choose("Sesión de origen", [*refs, scope, CANCEL])
        if choice == scope:
            cwd = None if cwd is not None else launch_directory
        elif choice == CANCEL:
            return None
        else:
            return refs[choice]


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


def run_wizard(bridge: Any, *, cwd: str | None = None) -> dict[str, Any]:
    """Select a source and target, inspect a preview, then request explicit execution."""
    ref = _source_session(bridge, cwd)
    if ref is None:
        return {"cancelled": True}
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


def run_home(bridge: Any, *, cwd: str | None = None) -> dict[str, Any]:
    """Keep linked conversations discoverable in the existing keyboard-first CLI."""
    from .sync import SyncService
    from .sync_cli import _ensure_worker

    service = SyncService(bridge=bridge)
    all_links = service.list()
    if not all_links:
        return run_wizard(bridge, cwd=cwd)
    launch_directory = str(Path.cwd())
    while True:
        links = (
            all_links
            if cwd is None
            else [
                link
                for link in all_links
                if any(
                    same_directory(endpoint.get("cwd") or "", cwd) for endpoint in link.get("endpoints", [])
                )
            ]
        )
        print("Carpeta: " + _visible(cwd) if cwd is not None else "Ámbito: todos los proyectos")
        labels = {
            f"{_visible(link['name'])} | {_visible(link['state'])} | {link['id']}": link for link in links
        }
        scope = ALL_PROJECTS if cwd is not None else CURRENT_PROJECT
        choice = _choose("Conversación o nueva conversión", ["Convertir otra sesión", *labels, scope, CANCEL])
        if choice == scope:
            cwd = None if cwd is not None else launch_directory
            continue
        if choice == CANCEL:
            return {"cancelled": True}
        if choice == "Convertir otra sesión":
            return run_wizard(bridge, cwd=cwd)
        break
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
