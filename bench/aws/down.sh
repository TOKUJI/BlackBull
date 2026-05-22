#!/usr/bin/env bash
# bench/aws/down.sh — tear down everything up.sh created.
#
# Refuses to run without a state file (no stray deletes).  Order:
#   1. terminate instance, wait until 'terminated'
#   2. delete security group
#   3. delete key pair (AWS) + local .pem
#   4. remove .state and .known_hosts
#   5. verify no tagged resources remain
#
# Each step is best-effort: if AWS reports the resource is already gone
# we log and continue.  The final verification step is what guarantees
# the user that nothing was left behind.

set -uo pipefail

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env || exit 1
_bench_aws_load_state || exit 1

echo "Tearing down:"
echo "  INSTANCE_ID = $INSTANCE_ID"
echo "  SG_ID       = $SG_ID"
echo "  KEY_NAME    = $KEY_NAME"
echo

# 1. Terminate instance ----------------------------------------------------
echo "Terminating instance $INSTANCE_ID ..."
if "${AWS_BASE[@]}" ec2 terminate-instances --instance-ids "$INSTANCE_ID" \
        >/dev/null 2>&1; then
    echo "Waiting for state=terminated ..."
    "${AWS_BASE[@]}" ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"
    echo "  terminated."
else
    echo "  (terminate returned non-zero; instance may already be gone)"
fi

# 2. Delete security group -------------------------------------------------
echo "Deleting security group $SG_ID ..."
# AWS sometimes needs a few seconds after instance termination before the
# ENI is released and the SG is deletable.  Retry briefly.
for _ in 1 2 3 4 5 6; do
    if "${AWS_BASE[@]}" ec2 delete-security-group --group-id "$SG_ID" >/dev/null 2>&1; then
        echo "  deleted."
        break
    fi
    sleep 5
done

# 3. Delete key pair -------------------------------------------------------
echo "Deleting key pair $KEY_NAME ..."
"${AWS_BASE[@]}" ec2 delete-key-pair --key-name "$KEY_NAME" >/dev/null 2>&1 \
    && echo "  deleted from AWS." \
    || echo "  (delete-key-pair returned non-zero; may already be gone)"

if [ -f "$LOCAL_KEY" ]; then
    rm -f "$LOCAL_KEY"
    echo "  removed local key file."
fi

# 4. Remove local state ----------------------------------------------------
rm -f "$STATE_FILE"
rm -f "$AWS_DIR/.known_hosts"
echo "Removed local state."

# 5. Verification ----------------------------------------------------------
echo
echo "Verifying no project resources remain ..."
LEFTOVERS=$("${AWS_BASE[@]}" ec2 describe-instances \
    --filters \
        "Name=tag:$TAG_KEY,Values=$TAG_VALUE" \
        "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text)
if [ -n "$LEFTOVERS" ] && [ "$LEFTOVERS" != "None" ]; then
    echo "WARNING: tagged instances still present: $LEFTOVERS" >&2
    echo "  manual cleanup:  aws ec2 terminate-instances --region $REGION --instance-ids $LEFTOVERS" >&2
    exit 1
fi

LEFTOVER_SG=$("${AWS_BASE[@]}" ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[].GroupId' --output text)
if [ -n "$LEFTOVER_SG" ] && [ "$LEFTOVER_SG" != "None" ]; then
    echo "WARNING: security group $SG_NAME still exists: $LEFTOVER_SG" >&2
    echo "  manual cleanup:  aws ec2 delete-security-group --region $REGION --group-id $LEFTOVER_SG" >&2
    exit 1
fi

LEFTOVER_KP=$("${AWS_BASE[@]}" ec2 describe-key-pairs \
    --filters "Name=key-name,Values=$KEY_NAME" \
    --query 'KeyPairs[].KeyName' --output text)
if [ -n "$LEFTOVER_KP" ] && [ "$LEFTOVER_KP" != "None" ]; then
    echo "WARNING: key pair $KEY_NAME still exists in AWS." >&2
    exit 1
fi

echo "  clean — no instances, security groups, or key pairs left."
