#!/usr/bin/env bash
# bench/aws/up.sh — provision the EC2 instance for a single AWS bench pass.
#
# Idempotent enough that a re-run after partial failure is safe: existing
# key pair / security group are reused if their names already match.  The
# only blocking failure is "an instance is already running and tagged for
# this project" — in which case the script refuses rather than orphan it.
#
# Side effects (all cleaned up by down.sh):
#   - aws ec2 create-key-pair (writes private key to bench/aws/<KEY_NAME>.pem)
#   - aws ec2 create-security-group + authorize-security-group-ingress
#   - aws ec2 run-instances (one instance, $INSTANCE_TYPE, tagged)
#   - bench/aws/.state holds INSTANCE_ID / PUBLIC_IP / SG_ID / AMI_ID

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

# --- Find a current Ubuntu 24.04 LTS AMI for the region -------------------
echo "Looking up latest Ubuntu 24.04 LTS AMI in $REGION ..."
AMI_ID=$("${AWS_BASE[@]}" ec2 describe-images \
    --owners "$AMI_OWNER" \
    --filters \
        "Name=name,Values=$AMI_NAME_PATTERN" \
        "Name=state,Values=available" \
        "Name=architecture,Values=x86_64" \
    --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
    --output text)
if [ -z "$AMI_ID" ] || [ "$AMI_ID" = "None" ]; then
    echo "bench/aws: no Ubuntu 24.04 AMI found in $REGION" >&2
    exit 1
fi
echo "  AMI: $AMI_ID"

# --- Key pair -------------------------------------------------------------
if "${AWS_BASE[@]}" ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
    echo "Key pair $KEY_NAME already exists in AWS."
    if [ ! -f "$LOCAL_KEY" ]; then
        echo "bench/aws: AWS has the key '$KEY_NAME' but the local .pem is missing." >&2
        echo "  We cannot recover the private key from AWS.  Delete the orphan with:" >&2
        echo "    aws ec2 delete-key-pair --region $REGION --key-name $KEY_NAME" >&2
        echo "  then re-run this script." >&2
        exit 1
    fi
else
    echo "Creating key pair $KEY_NAME ..."
    "${AWS_BASE[@]}" ec2 create-key-pair \
        --key-name "$KEY_NAME" \
        --tag-specifications "ResourceType=key-pair,Tags=[{Key=$TAG_KEY,Value=$TAG_VALUE}]" \
        --query 'KeyMaterial' \
        --output text > "$LOCAL_KEY"
    chmod 600 "$LOCAL_KEY"
    echo "  private key saved to $LOCAL_KEY (chmod 600)"
fi

# --- Security group: SSH from this machine's public IP only ---------------
MY_IP="$(curl -s --max-time 5 https://checkip.amazonaws.com)" || {
    echo "bench/aws: failed to look up this host's public IP via checkip.amazonaws.com" >&2
    exit 1
}
MY_CIDR="${MY_IP}/32"
echo "Ingress will be allowed from $MY_CIDR (port 22 only)."

if SG_ID=$("${AWS_BASE[@]}" ec2 describe-security-groups \
        --filters "Name=group-name,Values=$SG_NAME" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null) \
        && [ "$SG_ID" != "None" ] && [ -n "$SG_ID" ]; then
    echo "Security group $SG_NAME already exists: $SG_ID"
else
    echo "Creating security group $SG_NAME ..."
    SG_ID=$("${AWS_BASE[@]}" ec2 create-security-group \
        --group-name "$SG_NAME" \
        --description "BlackBull bench harness — temporary" \
        --tag-specifications "ResourceType=security-group,Tags=[{Key=$TAG_KEY,Value=$TAG_VALUE}]" \
        --query 'GroupId' --output text)
    echo "  group id: $SG_ID"
fi

# Add the SSH rule (no-op if already present — describe + grep first).
if ! "${AWS_BASE[@]}" ec2 describe-security-groups --group-ids "$SG_ID" \
        --query 'SecurityGroups[0].IpPermissions[?FromPort==`22`].IpRanges[].CidrIp' \
        --output text | grep -qF "$MY_CIDR"; then
    echo "Authorising ingress 22/tcp from $MY_CIDR ..."
    "${AWS_BASE[@]}" ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" \
        --protocol tcp --port 22 --cidr "$MY_CIDR" >/dev/null
fi

# --- Launch instance ------------------------------------------------------
echo "Launching $INSTANCE_TYPE in $REGION ..."
INSTANCE_ID=$("${AWS_BASE[@]}" ec2 run-instances \
    --image-id "$AMI_ID" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" \
    --security-group-ids "$SG_ID" \
    --block-device-mappings "[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":$VOLUME_SIZE_GB,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=$TAG_KEY,Value=$TAG_VALUE},{Key=Owner,Value=$USER}]" \
    --count 1 \
    --query 'Instances[0].InstanceId' --output text)
echo "  instance id: $INSTANCE_ID"

echo "Waiting for instance state=running ..."
"${AWS_BASE[@]}" ec2 wait instance-running --instance-ids "$INSTANCE_ID"

PUBLIC_IP=$("${AWS_BASE[@]}" ec2 describe-instances \
    --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "  public IP: $PUBLIC_IP"

# --- Wait for SSH ---------------------------------------------------------
echo "Waiting for SSH on $SSH_USER@$PUBLIC_IP ..."
deadline=$(( $(date +%s) + 180 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    if ssh "${SSH_OPTS[@]}" "$SSH_USER@$PUBLIC_IP" 'echo ok' >/dev/null 2>&1; then
        break
    fi
    sleep 5
done
if ! ssh "${SSH_OPTS[@]}" "$SSH_USER@$PUBLIC_IP" 'echo ok' >/dev/null 2>&1; then
    echo "bench/aws: SSH did not come up within 180s on $PUBLIC_IP" >&2
    echo "  instance is left running so you can investigate; tear down with:" >&2
    echo "    aws ec2 terminate-instances --region $REGION --instance-ids $INSTANCE_ID" >&2
    exit 1
fi
echo "  SSH ready."

# --- Persist state --------------------------------------------------------
umask 077
cat > "$STATE_FILE" <<EOF
# bench/aws/.state — written by up.sh on $(date -Iseconds)
INSTANCE_ID="$INSTANCE_ID"
PUBLIC_IP="$PUBLIC_IP"
SG_ID="$SG_ID"
AMI_ID="$AMI_ID"
EOF
echo
echo "Done.  Next: bash bench/aws/install.sh"
