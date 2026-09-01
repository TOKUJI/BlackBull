"""Build-context placeholder.

bench/aws/httparena_compare.sh generates the framework Dockerfile with
``COPY app.py launcher.py db.py grpc_bench.py /app/``, so this file must exist
in the staged framework dir for the image build to succeed.

The HTTPArena-side (upstream ``frameworks/blackbull/``) app.py does not import
this module — gRPC profiles are not subscribed — so the file carries no
runtime code.  The former gRPC implementation was part of BlackBull's diverged
HttpArena fork and was removed when the vendored integration was re-aligned
with upstream.
"""
