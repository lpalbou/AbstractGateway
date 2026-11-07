### Fixed

- **The release's container image build waits for PyPI instead of failing** (`docker/abstractgateway-server/install-gateway.sh`,
  both Dockerfiles). The v0.20.2 image job failed with "No matching distribution found for
  abstractgateway==0.20.2" minutes after the publish. The pinned PyPI install inside the image build is
  now retried with a bounded backoff (5 retries over about 5 minutes) before the build fails.
