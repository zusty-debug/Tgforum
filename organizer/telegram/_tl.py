"""Version-tolerant Telethon TL imports.

Newer Telethon releases moved the forum-topic calls from the
``channels`` namespace to ``messages``; older ones keep ``channels``.
"""
try:  # telethon >= ~1.41
    from telethon.tl.functions.messages import (CreateForumTopicRequest,
                                                GetForumTopicsRequest)
except ImportError:  # older telethon
    from telethon.tl.functions.channels import (CreateForumTopicRequest,
                                                GetForumTopicsRequest)

__all__ = ["CreateForumTopicRequest", "GetForumTopicsRequest"]
