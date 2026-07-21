"""ADK agents package.

Exports are lazy: importing ``adk_agents.prompts`` or
``adk_agents.schemas`` must NOT trigger ``agent.py`` (which pulls in
``google.adk`` and would create a circular import via ``programs.py``).
``app`` and ``root_agent`` are only needed by the ADK dev server, and
``AdkReviewWorkflow`` by the batch pipeline — both are resolved on first
access via ``__getattr__``.
"""
__all__ = ["AdkReviewWorkflow", "cancel_all_active", "app", "root_agent"]


def __getattr__(name):
    if name in ("app", "root_agent"):
        from .agent import app, root_agent

        return {"app": app, "root_agent": root_agent}[name]
    if name == "AdkReviewWorkflow":
        from .workflow import AdkReviewWorkflow

        return AdkReviewWorkflow
    if name == "cancel_all_active":
        from .workflow import cancel_all_active

        return cancel_all_active
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
