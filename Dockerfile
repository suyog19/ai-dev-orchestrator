FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

# Install system runtimes for multi-stack support (Phase 15)
# - git: required for cloning repos
# - maven: brings OpenJDK 17 as dependency (for Java Maven projects)
# - nodejs + npm: Node 18 LTS from Debian Bookworm (for Node/React projects)
# - curl + unzip: used to fetch Gradle distribution
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    unzip \
    maven \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install Gradle 8.5 (Debian apt version is 4.x, too old)
RUN curl -sL https://services.gradle.org/distributions/gradle-8.5-bin.zip \
        -o /tmp/gradle.zip \
    && unzip -q /tmp/gradle.zip -d /opt \
    && rm /tmp/gradle.zip

ENV GRADLE_HOME=/opt/gradle-8.5
ENV PATH="${GRADLE_HOME}/bin:${PATH}"

RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY config/ ./config/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
