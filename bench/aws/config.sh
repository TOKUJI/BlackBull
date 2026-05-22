# bench/aws/config.sh — shared configuration for the AWS bench harness.
#
# Sourced by up.sh / install.sh / run.sh / down.sh.  Keep environment
# variables here, never hard-code them in the action scripts.
#
# Override anything by exporting before invoking the action script:
#   REGION=us-west-2 bash bench/aws/up.sh
#
# Resources created by up.sh are tagged with Project=$TAG_VALUE so a
# describe-instances sweep can find leftovers if down.sh ever misses them.

# --- AWS targeting --------------------------------------------------------
: "${REGION:=us-east-1}"
: "${INSTANCE_TYPE:=c7i.xlarge}"
: "${VOLUME_SIZE_GB:=20}"     # /dev/sda1 root volume — generous for venv + apt

# --- Named resources (must be unique per concurrent run) ------------------
: "${KEY_NAME:=blackbull-bench-key}"
: "${SG_NAME:=blackbull-bench-sg}"
: "${TAG_KEY:=Project}"
: "${TAG_VALUE:=BlackBull-bench}"

# --- Local files ----------------------------------------------------------
AWS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AWS_DIR/../.." && pwd)"
STATE_FILE="$AWS_DIR/.state"          # INSTANCE_ID, PUBLIC_IP, SG_ID, AMI_ID
LOCAL_KEY="$AWS_DIR/${KEY_NAME}.pem"  # private SSH key — chmod 600 by up.sh

# --- Remote access --------------------------------------------------------
SSH_USER="ubuntu"
SSH_OPTS=(
    -i "$LOCAL_KEY"
    -o StrictHostKeyChecking=accept-new
    -o UserKnownHostsFile="$AWS_DIR/.known_hosts"
    -o ConnectTimeout=10
    -o ServerAliveInterval=30
)

# --- AMI selection --------------------------------------------------------
# Ubuntu 24.04 LTS (Noble) — owner 099720109477 is Canonical.
# Filter resolved at runtime in up.sh via aws ec2 describe-images.
AMI_OWNER="099720109477"
AMI_NAME_PATTERN="ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"

# --- aws CLI defaults -----------------------------------------------------
# Honour AWS_PROFILE if set; otherwise use whatever `aws configure` did.
AWS_BASE=(aws --region "$REGION" --output json)

# --- Sanity-checks (run when a script sources us) -------------------------
_bench_aws_require() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "bench/aws: missing required command: $1" >&2
        echo "  install hint: $2" >&2
        return 1
    }
}

_bench_aws_check_env() {
    _bench_aws_require aws  "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html" || return 1
    _bench_aws_require ssh  "apt-get install openssh-client" || return 1
    _bench_aws_require rsync "apt-get install rsync"          || return 1
    _bench_aws_require jq   "apt-get install jq"              || return 1

    # Confirm caller credentials work before we try to spend money.
    if ! "${AWS_BASE[@]}" sts get-caller-identity >/dev/null 2>&1; then
        echo "bench/aws: aws CLI cannot reach AWS — run 'aws configure' first" >&2
        return 1
    fi
}

# Helper used by all the action scripts.
_bench_aws_load_state() {
    [ -f "$STATE_FILE" ] || {
        echo "bench/aws: no state file at $STATE_FILE — has up.sh been run?" >&2
        return 1
    }
    # shellcheck disable=SC1090
    source "$STATE_FILE"
}
