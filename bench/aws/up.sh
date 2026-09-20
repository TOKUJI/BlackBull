#!/usr/bin/env bash
# bench/aws/up.sh — provision EC2 instance(s) for an AWS bench pass.
#
# TOPO=single (default): one $INSTANCE_TYPE host runs both the server
#   under test and the load tools (legacy single-host topology).
# TOPO=split:            two hosts in a cluster placement group — a
#   $INSTANCE_TYPE server and a $LOADGEN_INSTANCE_TYPE load generator
#   talking over VPC private networking.
#
# Idempotent: existing key pair / security group / placement group are
# reused if their names already match.  The only blocking failure is
# "tagged resources already exist for this project" — the script refuses
# rather than orphan or duplicate.
#
# Side effects (all cleaned up by down.sh):
#   - aws ec2 create-key-pair                (writes private key locally)
#   - aws ec2 create-security-group + authorize-security-group-ingress
#   - aws ec2 create-placement-group         (TOPO=split only)
#   - aws ec2 run-instances                  (1 or 2 instances)
#   - bench/aws/.state                       (full topology snapshot)

set -euo pipefail

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env

# --- Refuse to clobber an existing run ------------------------------------
if [ -f "$STATE_FILE" ]; then
    echo "bench/aws: $STATE_FILE already exists; tear down first with:" >&2
    echo "  bash bench/aws/down.sh" >&2
    exit 1
fi
rm -f "${STATE_FILE}.clean"

case "$TOPO" in
    single|split) ;;
    *) echo "bench/aws: TOPO must be 'single' or 'split' (got '$TOPO')" >&2; exit 1 ;;
esac
echo "Topology: $TOPO"

AMI_ID=""
SG_ID=""
SERVER_INSTANCE_ID=""
SERVER_PUBLIC_IP=""
SERVER_PRIVATE_IP=""
LOADGEN_INSTANCE_ID=""
LOADGEN_PUBLIC_IP=""
LOADGEN_PRIVATE_IP=""
RUN_TOKEN="${RUN_TOKEN:-$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM}"
KEY_PAIR_OWNED=0
SECURITY_GROUP_OWNED=0
PLACEMENT_GROUP_OWNED=0

persist_state() {
    local temporary
    umask 077
    temporary="$(mktemp "${STATE_FILE}.tmp.XXXXXX")"
    cat > "$temporary" <<EOF
# bench/aws state written by up.sh
TOPO="$TOPO"
SG_ID="$SG_ID"
AMI_ID="$AMI_ID"
PLACEMENT_GROUP_NAME="$PLACEMENT_GROUP_NAME"
SERVER_INSTANCE_ID="$SERVER_INSTANCE_ID"
SERVER_PUBLIC_IP="$SERVER_PUBLIC_IP"
SERVER_PRIVATE_IP="$SERVER_PRIVATE_IP"
LOADGEN_INSTANCE_ID="$LOADGEN_INSTANCE_ID"
LOADGEN_PUBLIC_IP="$LOADGEN_PUBLIC_IP"
LOADGEN_PRIVATE_IP="$LOADGEN_PRIVATE_IP"
RUN_TAG_KEY="$RUN_TAG_KEY"
RUN_TOKEN="$RUN_TOKEN"
KEY_PAIR_OWNED="$KEY_PAIR_OWNED"
SECURITY_GROUP_OWNED="$SECURITY_GROUP_OWNED"
PLACEMENT_GROUP_OWNED="$PLACEMENT_GROUP_OWNED"
INSTANCE_ID="$SERVER_INSTANCE_ID"
PUBLIC_IP="$SERVER_PUBLIC_IP"
EOF
    mv "$temporary" "$STATE_FILE"
}

cleanup_failed_provision() {
    local rc=$?
    trap - EXIT HUP INT TERM
    if [ "$rc" -ne 0 ] && [ -f "$STATE_FILE" ]; then
        echo "bench/aws: provisioning failed; cleaning recorded resources" >&2
        if ! STATE_FILE="$STATE_FILE" bash "$(dirname "$0")/down.sh"; then
            rc=1
        fi
    fi
    exit "$rc"
}
trap cleanup_failed_provision EXIT
trap 'exit 130' HUP INT TERM

# --- Find a current Ubuntu 24.04 LTS AMI for the region -------------------
echo "Looking up latest Ubuntu 24.04 LTS AMI in $REGION ..."
AMI_ID=$("${AWS_BASE[@]}" ec2 describe-images \
    --owners "$AMI_OWNER" \
    --filters \
        "Name=name,Values=$AMI_NAME_PATTERN" \
        "Name=state,Values=available" \
        "Name=architecture,Values=${AMI_ARCH:-x86_64}" \
    --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
    --output text)
if [ -z "$AMI_ID" ] || [ "$AMI_ID" = "None" ]; then
    echo "bench/aws: no Ubuntu 24.04 AMI found in $REGION" >&2
    exit 1
fi
echo "  AMI: $AMI_ID"

# --- Key pair -------------------------------------------------------------
if ! existing_key=$("${AWS_BASE[@]}" ec2 describe-key-pairs \
        --filters "Name=key-name,Values=$KEY_NAME" \
        --query 'KeyPairs[].KeyName' --output text); then
    echo "bench/aws: failed to inspect key pair $KEY_NAME" >&2
    exit 1
fi
if [ -n "$existing_key" ] && [ "$existing_key" != "None" ]; then
    echo "Key pair $KEY_NAME already exists in AWS."
    if [ ! -f "$LOCAL_KEY" ]; then
        echo "bench/aws: AWS has the key '$KEY_NAME' but the local .pem is missing." >&2
        echo "  We cannot recover the private key from AWS.  Delete the orphan with:" >&2
        echo "    aws ec2 delete-key-pair --region $REGION --key-name $KEY_NAME" >&2
        echo "  then re-run this script." >&2
        exit 1
    fi
else
    KEY_PAIR_OWNED=1
    persist_state
    echo "Creating key pair $KEY_NAME ..."
    "${AWS_BASE[@]}" ec2 create-key-pair \
        --key-name "$KEY_NAME" \
        --tag-specifications "ResourceType=key-pair,Tags=[{Key=$TAG_KEY,Value=$TAG_VALUE}]" \
        --query 'KeyMaterial' \
        --output text > "$LOCAL_KEY"
    chmod 600 "$LOCAL_KEY"
    echo "  private key saved to $LOCAL_KEY (chmod 600)"
fi
persist_state

# --- Security group: SSH from this machine's public IP only ---------------
MY_IP="$(curl -s --max-time 5 https://checkip.amazonaws.com)" || {
    echo "bench/aws: failed to look up this host's public IP via checkip.amazonaws.com" >&2
    exit 1
}
MY_CIDR="${MY_IP}/32"
echo "Ingress will be allowed from $MY_CIDR (port 22 only)."

if ! SG_ID=$("${AWS_BASE[@]}" ec2 describe-security-groups \
        --filters "Name=group-name,Values=$SG_NAME" \
        --query 'SecurityGroups[0].GroupId' --output text); then
    echo "bench/aws: failed to inspect security group $SG_NAME" >&2
    exit 1
fi
if [ "$SG_ID" != "None" ] && [ -n "$SG_ID" ]; then
    echo "Security group $SG_NAME already exists: $SG_ID"
else
    SG_ID=""
    SECURITY_GROUP_OWNED=1
    persist_state
    echo "Creating security group $SG_NAME ..."
    SG_ID=$("${AWS_BASE[@]}" ec2 create-security-group \
        --group-name "$SG_NAME" \
        --description "BlackBull bench harness (temporary)" \
        --tag-specifications "ResourceType=security-group,Tags=[{Key=$TAG_KEY,Value=$TAG_VALUE}]" \
        --query 'GroupId' --output text)
    echo "  group id: $SG_ID"
fi
persist_state

# Add the SSH rule (no-op if already present — describe + grep first).
if ! "${AWS_BASE[@]}" ec2 describe-security-groups --group-ids "$SG_ID" \
        --query "SecurityGroups[0].IpPermissions[?IpProtocol=='tcp' && FromPort==\`22\` && ToPort==\`22\`].IpRanges[].CidrIp" \
        --output text | grep -qF "$MY_CIDR"; then
    if [ "$SECURITY_GROUP_OWNED" != "1" ]; then
        echo "bench/aws: existing security group lacks SSH ingress for $MY_CIDR; refusing to modify it" >&2
        exit 1
    fi
    echo "Authorising ingress 22/tcp from $MY_CIDR ..."
    "${AWS_BASE[@]}" ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" \
        --protocol tcp --port 22 --cidr "$MY_CIDR" >/dev/null
fi

# Split-topology only: allow intra-SG TCP traffic for two purposes —
#   1. Port 22, so the loadgen can SSH into the server to drive the
#      remote-lifecycle hook (`BENCH_REMOTE_LIFECYCLE=1`).
#   2. Ports 8000-9100, the bench port range that the load tools target.
# Neither rule exposes anything to the internet — both restrict source
# to the same SG, so only members of the SG can use them.
if [ "$TOPO" = "split" ]; then
    if ! "${AWS_BASE[@]}" ec2 describe-security-groups --group-ids "$SG_ID" \
            --query "SecurityGroups[0].IpPermissions[?IpProtocol=='tcp' && FromPort==\`22\` && ToPort==\`22\`].UserIdGroupPairs[].GroupId" \
            --output text | grep -qF "$SG_ID"; then
        if [ "$SECURITY_GROUP_OWNED" != "1" ]; then
            echo "bench/aws: existing security group lacks intra-group SSH ingress; refusing to modify it" >&2
            exit 1
        fi
        echo "Authorising intra-SG ingress 22/tcp (loadgen → server SSH) ..."
        "${AWS_BASE[@]}" ec2 authorize-security-group-ingress \
            --group-id "$SG_ID" \
            --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":22,\"ToPort\":22,\"UserIdGroupPairs\":[{\"GroupId\":\"$SG_ID\"}]}]" \
            >/dev/null
    fi
    if ! "${AWS_BASE[@]}" ec2 describe-security-groups --group-ids "$SG_ID" \
            --query "SecurityGroups[0].IpPermissions[?IpProtocol=='tcp' && FromPort==\`8000\` && ToPort==\`9100\`].UserIdGroupPairs[].GroupId" \
            --output text | grep -qF "$SG_ID"; then
        if [ "$SECURITY_GROUP_OWNED" != "1" ]; then
            echo "bench/aws: existing security group lacks benchmark ingress; refusing to modify it" >&2
            exit 1
        fi
        echo "Authorising intra-SG ingress 8000-9100/tcp (loadgen → server bench ports) ..."
        "${AWS_BASE[@]}" ec2 authorize-security-group-ingress \
            --group-id "$SG_ID" \
            --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":8000,\"ToPort\":9100,\"UserIdGroupPairs\":[{\"GroupId\":\"$SG_ID\"}]}]" \
            >/dev/null
    fi
fi

# --- Placement group (split topology only) --------------------------------
PLACEMENT_ARGS=()
if [ "$TOPO" = "split" ]; then
    if ! existing_pg=$("${AWS_BASE[@]}" ec2 describe-placement-groups \
            --filters "Name=group-name,Values=$PLACEMENT_GROUP_NAME" \
            --query 'PlacementGroups[].GroupName' --output text); then
        echo "bench/aws: failed to inspect placement group $PLACEMENT_GROUP_NAME" >&2
        exit 1
    fi
    if [ -n "$existing_pg" ] && [ "$existing_pg" != "None" ]; then
        echo "Placement group $PLACEMENT_GROUP_NAME already exists."
    else
        PLACEMENT_GROUP_OWNED=1
        persist_state
        echo "Creating cluster placement group $PLACEMENT_GROUP_NAME ..."
        "${AWS_BASE[@]}" ec2 create-placement-group \
            --group-name "$PLACEMENT_GROUP_NAME" \
            --strategy cluster \
            --tag-specifications "ResourceType=placement-group,Tags=[{Key=$TAG_KEY,Value=$TAG_VALUE}]" \
            >/dev/null
    fi
    PLACEMENT_ARGS=(--placement "GroupName=$PLACEMENT_GROUP_NAME")
fi

# --- Launch instances -----------------------------------------------------
# launch_one <role> <instance-type> <SERVER|LOADGEN>
launch_one() {
    local role="$1" itype="$2" prefix="$3"
    local -n instance_id="${prefix}_INSTANCE_ID"
    local -n public_ip="${prefix}_PUBLIC_IP"
    local -n private_ip="${prefix}_PRIVATE_IP"
    instance_id=$("${AWS_BASE[@]}" ec2 run-instances \
        --image-id "$AMI_ID" \
        --instance-type "$itype" \
        --key-name "$KEY_NAME" \
        --security-group-ids "$SG_ID" \
        --client-token "${RUN_TOKEN}-${role}" \
        --instance-initiated-shutdown-behavior terminate \
        "${PLACEMENT_ARGS[@]}" \
        --block-device-mappings "[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":$VOLUME_SIZE_GB,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=$TAG_KEY,Value=$TAG_VALUE},{Key=$RUN_TAG_KEY,Value=$RUN_TOKEN},{Key=Owner,Value=${USER:-unknown}},{Key=$ROLE_TAG_KEY,Value=$role}]" \
        --count 1 \
        --query 'Instances[0].InstanceId' --output text)
    persist_state
    echo "  $role instance id: $instance_id" >&2
    "${AWS_BASE[@]}" ec2 wait instance-running --instance-ids "$instance_id" >&2
    public_ip=$("${AWS_BASE[@]}" ec2 describe-instances --instance-ids "$instance_id" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
    private_ip=$("${AWS_BASE[@]}" ec2 describe-instances --instance-ids "$instance_id" \
        --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)
    echo "  $role public IP:  $public_ip"  >&2
    echo "  $role private IP: $private_ip" >&2
    persist_state
}

wait_for_ssh() {
    local ip="$1" role="$2"
    echo "Waiting for SSH on $SSH_USER@$ip ($role) ..."
    local deadline
    deadline=$(( $(date +%s) + 180 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if ssh "${SSH_OPTS[@]}" "$SSH_USER@$ip" 'echo ok' >/dev/null 2>&1; then
            echo "  SSH ready ($role)."
            return 0
        fi
        sleep 5
    done
    echo "bench/aws: SSH did not come up within 180s on $ip ($role)" >&2
    return 1
}

echo "Launching server ($INSTANCE_TYPE) in $REGION ..."
launch_one "$SERVER_ROLE_VALUE" "$INSTANCE_TYPE" SERVER

if [ "$TOPO" = "split" ]; then
    echo "Launching load generator ($LOADGEN_INSTANCE_TYPE) in $REGION ..."
    launch_one "$LOADGEN_ROLE_VALUE" "$LOADGEN_INSTANCE_TYPE" LOADGEN
fi

wait_for_ssh "$SERVER_PUBLIC_IP" "server" || {
    echo "  server did not become reachable; automatic cleanup will run." >&2
    exit 1
}
if [ "$TOPO" = "split" ]; then
    wait_for_ssh "$LOADGEN_PUBLIC_IP" "loadgen" || {
        echo "  loadgen did not become reachable; automatic cleanup will run." >&2
        exit 1
    }
fi

# --- Persist final state --------------------------------------------------
persist_state
trap - EXIT HUP INT TERM
echo
echo "Done.  Next: bash bench/aws/install.sh"
