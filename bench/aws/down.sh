#!/usr/bin/env bash
# bench/aws/down.sh — tear down everything up.sh created.
#
# Refuses to run without a state file (no stray deletes).  Order:
#   1. terminate all instances (server + optional loadgen), wait
#   2. delete placement group if TOPO=split (must be empty first)
#   3. delete security group
#   4. delete key pair (AWS) + local .pem
#   5. remove .state and .known_hosts
#   6. verify no tagged resources remain
#
# Each step is best-effort: if AWS reports the resource is already gone
# we log and continue.  The final verification step is what guarantees
# the user that nothing was left behind.

set -uo pipefail

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env || exit 1
_bench_aws_load_state || exit 1

# Backward-compat: pre-Sprint-20 state files only have INSTANCE_ID/PUBLIC_IP.
SERVER_INSTANCE_ID="${SERVER_INSTANCE_ID:-${INSTANCE_ID:-}}"
LOADGEN_INSTANCE_ID="${LOADGEN_INSTANCE_ID:-}"
TOPO="${TOPO:-single}"
PLACEMENT_GROUP_NAME="${PLACEMENT_GROUP_NAME:-blackbull-bench-cpg}"

echo "Tearing down:"
echo "  TOPO         = $TOPO"
echo "  SERVER       = $SERVER_INSTANCE_ID"
[ -n "$LOADGEN_INSTANCE_ID" ] && echo "  LOADGEN      = $LOADGEN_INSTANCE_ID"
echo "  SG_ID        = $SG_ID"
echo "  KEY_NAME     = $KEY_NAME"
[ "$TOPO" = "split" ] && echo "  PG_NAME      = $PLACEMENT_GROUP_NAME"
echo

# 1. Terminate instances ---------------------------------------------------
INSTANCE_IDS=()
[ -n "$SERVER_INSTANCE_ID" ]  && INSTANCE_IDS+=("$SERVER_INSTANCE_ID")
[ -n "$LOADGEN_INSTANCE_ID" ] && INSTANCE_IDS+=("$LOADGEN_INSTANCE_ID")

if [ "${#INSTANCE_IDS[@]}" -gt 0 ]; then
    echo "Terminating instance(s): ${INSTANCE_IDS[*]} ..."
    if "${AWS_BASE[@]}" ec2 terminate-instances --instance-ids "${INSTANCE_IDS[@]}" \
            >/dev/null 2>&1; then
        echo "Waiting for state=terminated ..."
        "${AWS_BASE[@]}" ec2 wait instance-terminated --instance-ids "${INSTANCE_IDS[@]}"
        echo "  terminated."
    else
        echo "  (terminate returned non-zero; instance(s) may already be gone)"
    fi
fi

# 2. Delete placement group (only if it exists; must be empty) -------------
if [ "$TOPO" = "split" ]; then
    echo "Deleting placement group $PLACEMENT_GROUP_NAME ..."
    if "${AWS_BASE[@]}" ec2 describe-placement-groups --group-names "$PLACEMENT_GROUP_NAME" >/dev/null 2>&1; then
        for _ in 1 2 3 4 5 6; do
            if "${AWS_BASE[@]}" ec2 delete-placement-group --group-name "$PLACEMENT_GROUP_NAME" >/dev/null 2>&1; then
                echo "  deleted."
                break
            fi
            sleep 5
        done
    else
        echo "  (placement group already gone)"
    fi
fi

# 3. Delete security group -------------------------------------------------
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

# 4. Delete key pair -------------------------------------------------------
echo "Deleting key pair $KEY_NAME ..."
"${AWS_BASE[@]}" ec2 delete-key-pair --key-name "$KEY_NAME" >/dev/null 2>&1 \
    && echo "  deleted from AWS." \
    || echo "  (delete-key-pair returned non-zero; may already be gone)"

if [ -f "$LOCAL_KEY" ]; then
    rm -f "$LOCAL_KEY"
    echo "  removed local key file."
fi

# 5. Remove local state ----------------------------------------------------
rm -f "$STATE_FILE"
rm -f "$AWS_DIR/.known_hosts"
echo "Removed local state."

# 6. Verification ----------------------------------------------------------
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

LEFTOVER_PG=$("${AWS_BASE[@]}" ec2 describe-placement-groups \
    --filters "Name=group-name,Values=$PLACEMENT_GROUP_NAME" \
    --query 'PlacementGroups[].GroupName' --output text)
if [ -n "$LEFTOVER_PG" ] && [ "$LEFTOVER_PG" != "None" ]; then
    echo "WARNING: placement group $PLACEMENT_GROUP_NAME still exists." >&2
    echo "  manual cleanup:  aws ec2 delete-placement-group --region $REGION --group-name $PLACEMENT_GROUP_NAME" >&2
    exit 1
fi

echo "  clean — no instances, security groups, key pairs, or placement groups left."
