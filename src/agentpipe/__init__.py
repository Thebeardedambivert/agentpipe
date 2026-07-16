"""agentpipe: an agentic coding pipeline, built in layers.

Loads .env on import so every entry point gets the same config without each one
remembering to. Layer 1 will care about this a lot: config that is loaded in
some paths and not others is exactly the kind of implicit context P1 is about.
"""

from dotenv import load_dotenv

load_dotenv()
