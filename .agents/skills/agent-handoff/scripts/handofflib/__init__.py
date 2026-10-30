"""agent-handoff core library.

Standard library only. Kept free of any agent/product specific API so that
DSH, Codex, Claude or a future agent can run the same scripts unchanged.

Truth order enforced by this library:

    1. real code on disk
    2. git (branch / HEAD / status / diff)
    3. .agent-state checkpoint descriptions

Checkpoints are an index for recovery, never a claim of truth.
"""

__version__ = "0.1.0"

SCHEMA_VERSION = 1
