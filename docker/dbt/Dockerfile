# The dbt runner: dbt-fabric plus Microsoft's ODBC Driver 18.
#
# THIS IS NOT AN EMULATOR WORKAROUND. Microsoft's own documentation for the
# dbt-fabric adapter states it plainly: "There is no pure-Python or driver-free
# option — an ODBC driver is mandatory." That is true against
# api.fabric.microsoft.com, not just against the local family.
#
# The driver lives in an image rather than on a contributor's machine because
# `git clone && make` must work on Windows, macOS and Linux, and a native
# system-level install is the one thing make cannot do. In real Fabric this is
# also how dbt usually runs: dbt is never hosted INSIDE Fabric, so it executes
# on a laptop, a CI agent, or a container — and a container is the shape any
# repeatable pipeline picks.
FROM mirror.gcr.io/library/python:3.12-slim

# msodbcsql18 from Microsoft's repository. Dearmoring the key to a file and
# installing it with `install` avoids the gpg --dearmor path that needs a tty.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates unixodbc-dev build-essential \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    # Purge the build-only tools, but NOT the driver's runtime dependencies.
    # `autoremove` after this took libgssapi_krb5 with it, and unixODBC then
    # reported `Can't open lib … : file not found` for a library that was
    # plainly present — dlopen failing on a MISSING DEPENDENCY reads as the
    # driver itself being absent, which sends you looking in the wrong place.
    && apt-get purge -y build-essential curl gnupg \
    && rm -rf /var/lib/apt/lists/*

# Pinned, like every other version in this repository: an adapter that resolves
# differently between two runs of the same commit is not reproducible.
RUN pip install --no-cache-dir "dbt-core==1.9.*" "dbt-fabric==1.9.*"

WORKDIR /gold
ENTRYPOINT ["dbt"]
