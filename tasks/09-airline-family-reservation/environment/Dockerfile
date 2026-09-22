# Airline voice-agent environment.
#
# The image carries the schema and the SQL that builds the world; the database
# itself is constructed at container start by task-init.sh. Two runs of this
# image therefore begin from identical state, and a run can be reset without a
# rebuild.
FROM postgres:16-bookworm

# Everything the server and the verifier need at runtime, installed while there
# is still a network. Nothing below this line reaches out again.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl jq procps python3 python3-psycopg2 ripgrep \
        sudo tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Etc/UTC \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN useradd --create-home --shell /bin/bash agent

# The tool server: readable and runnable, but not the deliverable. It stands in
# for the airline's reservation backend the way a real integration would.
RUN install -d -m 0755 -o root -g root /opt/tool-server
COPY --chown=root:root --chmod=0444 server/ /opt/tool-server/
RUN chmod 0555 /opt/tool-server

# The SQL that builds the world. Readable, so an operator can see exactly what
# state a run started from.
RUN install -d -m 0755 -o root -g root /opt/sql
COPY --chown=root:root --chmod=0444 sql/ /opt/sql/

# Verifier-only material: the recorded call sequence and the expected final
# state. The agent account sees an opaque parent directory and nothing inside it.
# The generator that produced the population never enters the image at all.
RUN install -d -m 0700 -o root -g root /var/lib/task-data /var/lib/task-data/verifier
COPY --chown=root:root --chmod=600 verifier-data/ /var/lib/task-data/verifier/

# The agent's workspace: the governing policy, readable but not writable, so a
# run cannot quietly relax the rules it is being held to.
RUN install -d -m 0755 -o agent -g agent /workspace
COPY --chown=root:root --chmod=0444 workspace/ /workspace/

# Where the run records what it said to the caller. Agent-writable, unlike the
# policy beside it, because the grading layer has no other way to see the half of
# this call that happens in speech.
RUN install -m 0666 -o agent -g agent /dev/null /workspace/transcript.txt

# Where a run's final state is collected. Mount a host directory here to keep it.
RUN install -d -m 0777 -o root -g root /out
VOLUME ["/out"]

COPY --chown=root:root task-init.sh /usr/local/bin/task-init.sh
RUN chmod 0755 /usr/local/bin/task-init.sh

# PGDATA lives outside the image so the cluster is created fresh on boot and the
# directory can be mounted for inspection.
ENV PGDATA=/var/lib/postgresql/data \
    POSTGRES_DB=voiceenv \
    POSTGRES_USER=voiceenv \
    TOOL_SERVER_URL=http://127.0.0.1:8080 \
    STATE_OUT_DIR=/out

WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/task-init.sh"]
CMD ["--wait"]
