FROM dailyco/pipecat-base:latest

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Uncomment if you wish to print a summary of the features available in the base image
# ENV PCC_LOG_FEATURES_SUMMARY=true

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Copy the application code. The deployable agent is the Claude triage bot,
# copied to bot.py (the entrypoint the base image runs). Bring its modules too.
COPY ./bot-claude.py bot.py
COPY ./triage.py triage.py
COPY ./triage_state.py triage_state.py
COPY ./conf_filter.py conf_filter.py
COPY ./tts_cache.py tts_cache.py
COPY ./nemotron_llm.py nemotron_llm.py
COPY ./transcript_tap.py transcript_tap.py
COPY ./events.py events.py
COPY ./mock_backend.py mock_backend.py
COPY ./tools.py tools.py
COPY ./telephony.py telephony.py