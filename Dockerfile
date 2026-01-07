FROM ascendai/python:3.11-ubuntu22.04

RUN apt-get update -y && apt-get install curl git gcc g++ cmake libnuma-dev jq -y

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/${TARGETARCH}/kubectl" \
 && chmod +x kubectl \
 && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl

# Install jinja2-cli
RUN pip install jinja2-cli modelscope
