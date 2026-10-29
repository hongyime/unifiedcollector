"""Runtime read-only guards for platform clients.

Defence-in-depth: the static tripwire (tests/test_readonly_guard.py) catches
outbound method names at PR time; these wrappers raise at runtime if a write
method is invoked despite passing the static check (e.g. via dynamic dispatch).
"""


class ReadOnlyTelegramClient:
    """Wraps a telethon.TelegramClient, blocking write operations at runtime."""

    FORBIDDEN = frozenset({
        "send_message", "send_file", "send_read_acknowledge",
        "edit_message", "delete_messages",
        "forward_messages", "send_reaction",
        "pin_message", "unpin_message",
        "kick_participant", "edit_admin", "edit_permissions",
    })

    # Raw TL requests that mutate chat membership (GAP-6). ``__call__`` is the
    # generic ``client(request)`` RPC escape hatch the method-name guard above
    # cannot see, so these are rejected by request class name. Fail closed:
    # raising, never log-and-forward. ``CheckChatInviteRequest`` stays allowed
    # (read-only invite probe).
    FORBIDDEN_REQUESTS = frozenset({
        "JoinChannelRequest",
        "ImportChatInviteRequest",
        "LeaveChannelRequest",
    })

    def __init__(self, client):
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name):
        if name in ReadOnlyTelegramClient.FORBIDDEN:
            raise RuntimeError(
                f"Write operation blocked: TelegramClient.{name}() "
                f"is forbidden in read-only collector mode"
            )
        return getattr(object.__getattribute__(self, "_client"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_client"), name, value)

    def __call__(self, *args, **kwargs):
        request = args[0] if args else None
        if request is not None and (
            type(request).__name__ in ReadOnlyTelegramClient.FORBIDDEN_REQUESTS
        ):
            raise RuntimeError(
                f"Membership write blocked: {type(request).__name__}() "
                f"is forbidden in read-only collector mode"
            )
        return object.__getattribute__(self, "_client")(*args, **kwargs)

    def __aenter__(self):
        return object.__getattribute__(self, "_client").__aenter__()

    def __aexit__(self, *args):
        return object.__getattribute__(self, "_client").__aexit__(*args)
