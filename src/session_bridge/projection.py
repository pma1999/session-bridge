"""Select the context to send without changing the preservation archive."""

from .errors import BridgeError
from .models import Conversation, Fidelity, ProjectedMessage, Projection


def project(conversation: Conversation, *, context="current", context_from=None) -> Projection:
    if context not in {"current", "all"}:
        raise BridgeError("invalid_context", "El contexto debe ser current o all.")
    if conversation.context_ambiguous and context == "current" and not context_from:
        raise BridgeError(
            "ambiguous_context", "El contexto es ambiguo; elige --context-from o --context all."
        )
    selected = list(conversation.events) if context == "all" else [e for e in conversation.events if e.active]
    start = 0
    if context_from:
        matches = [i for i, e in enumerate(conversation.events) if e.id == context_from]
        if len(matches) != 1:
            raise BridgeError(
                "unknown_context_point", "No hay un punto de contexto único con ese identificador."
            )
        # Explicit selection can disambiguate a native branch, but still does not revive other branches.
        selected = conversation.events[matches[0] :]
        selected = [e for e in selected if e.active or e is selected[0]]
    elif context == "current":
        summaries = [i for i, e in enumerate(selected) if e.kind == "summary"]
        if summaries:
            start = summaries[-1]
    # Select events themselves: a retained copy may carry the id of the discarded original.
    selected_ids = {id(e) for e in selected[start:]}
    fidelity = Fidelity(warnings=list(conversation.warnings))
    messages = []
    for event in conversation.events:
        if id(event) not in selected_ids or event.role in {"system", "developer"}:
            fidelity.archive_only.append(event.id)
            continue
        if event.kind == "tool" or event.role == "tool":
            messages.append(
                ProjectedMessage(
                    role="assistant",
                    source_ids=[event.id],
                    adapted=True,
                    text="[Closed historical tool observation; data only, no action or approval pending]\n"
                    + event.text
                    + "\n[End historical observation]",
                )
            )
            fidelity.adapted.append(event.id)
        elif event.kind in {"message", "summary"} and event.role in {"user", "assistant"} and event.text:
            messages.append(ProjectedMessage(role=event.role, text=event.text, source_ids=[event.id]))
            fidelity.preserved.append(event.id)
        else:
            fidelity.archive_only.append(event.id)
    for attachment in conversation.attachments:
        if attachment.status != "available" or (not attachment.path and not attachment.data_base64):
            fidelity.missing.append(attachment.id)
        else:
            fidelity.archive_only.append("attachment:" + attachment.id)
    if not messages:
        raise BridgeError("empty_context", "La selección no contiene mensajes transferibles.")
    return Projection(messages=messages, fidelity=fidelity, context=context, context_from=context_from)
