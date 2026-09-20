#!/usr/bin/env bash
# bench/aws/down.sh — tear down everything up.sh created.
#
# Refuses to run without a state file (no stray deletes).  Order:
#   1. terminate all instances (server + optional loadgen), wait
#   2. delete placement group if TOPO=split (must be empty first)
#   3. delete security group
#   4. delete key pair in AWS
#   5. verify no tagged resources remain
#   6. remove local artifacts and publish teardown proof
#
# Each step is best-effort: if AWS reports the resource is already gone
# we log and continue.  The final verification step is what guarantees
# the user that nothing was left behind.

set -uo pipefail

# shellcheck source=config.sh
source "$(dirname "$0")/config.sh"
_bench_aws_check_env || exit 1
_bench_aws_load_state || exit 1
TEARDOWN_PROOF="${STATE_FILE}.clean"
rm -f "$TEARDOWN_PROOF"

# Backward-compat: legacy state files only have INSTANCE_ID/PUBLIC_IP.
SERVER_INSTANCE_ID="${SERVER_INSTANCE_ID:-${INSTANCE_ID:-}}"
LOADGEN_INSTANCE_ID="${LOADGEN_INSTANCE_ID:-}"
TOPO="${TOPO:-single}"
PLACEMENT_GROUP_NAME="${PLACEMENT_GROUP_NAME:-blackbull-bench-cpg}"
RUN_TOKEN="${RUN_TOKEN:-}"
KEY_PAIR_OWNED="${KEY_PAIR_OWNED:-1}"
SECURITY_GROUP_OWNED="${SECURITY_GROUP_OWNED:-1}"
PLACEMENT_GROUP_OWNED="${PLACEMENT_GROUP_OWNED:-1}"

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
verified=1
add_instance_id() {
    local candidate="$1" existing
    [ -n "$candidate" ] && [ "$candidate" != "None" ] || return 0
    for existing in "${INSTANCE_IDS[@]}"; do
        [ "$existing" != "$candidate" ] || return 0
    done
    INSTANCE_IDS+=("$candidate")
}
add_instance_id "$SERVER_INSTANCE_ID"
add_instance_id "$LOADGEN_INSTANCE_ID"

if [ -n "$RUN_TOKEN" ]; then
    if ! DISCOVERED_INSTANCES=$("${AWS_BASE[@]}" ec2 describe-instances \
        --filters \
            "Name=tag:$RUN_TAG_KEY,Values=$RUN_TOKEN" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query 'Reservations[].Instances[].InstanceId' --output text); then
        echo "WARNING: failed to discover instances for this run." >&2
        verified=0
    else
        for instance_id in $DISCOVERED_INSTANCES; do
            add_instance_id "$instance_id"
        done
    fi
fi

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

# 2. Delete placement group (only if this run created it) ------------------
if [ "$TOPO" = "split" ] && [ "$PLACEMENT_GROUP_OWNED" = "1" ]; then
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
if [ "$SECURITY_GROUP_OWNED" = "1" ] && [ -z "$SG_ID" ]; then
    if ! SG_ID=$("${AWS_BASE[@]}" ec2 describe-security-groups \
            --filters "Name=group-name,Values=$SG_NAME" \
            --query 'SecurityGroups[0].GroupId' --output text); then
        echo "WARNING: failed to recover security group id." >&2
        verified=0
    elif [ "$SG_ID" = "None" ]; then
        SG_ID=""
    fi
fi
if [ "$SECURITY_GROUP_OWNED" = "1" ] && [ -n "$SG_ID" ]; then
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
fi

# 4. Delete key pair -------------------------------------------------------
if [ "$KEY_PAIR_OWNED" = "1" ]; then
    echo "Deleting key pair $KEY_NAME ..."
    "${AWS_BASE[@]}" ec2 delete-key-pair --key-name "$KEY_NAME" >/dev/null 2>&1 \
        && echo "  deleted from AWS." \
        || echo "  (delete-key-pair returned non-zero; may already be gone)"
fi

# 5. Verification ----------------------------------------------------------
echo
echo "Verifying no project resources remain ..."
INSTANCE_TAG_FILTER="Name=tag:$TAG_KEY,Values=$TAG_VALUE"
if [ -n "$RUN_TOKEN" ]; then
    INSTANCE_TAG_FILTER="Name=tag:$RUN_TAG_KEY,Values=$RUN_TOKEN"
fi
if ! LEFTOVERS=$("${AWS_BASE[@]}" ec2 describe-instances \
    --filters \
        "$INSTANCE_TAG_FILTER" \
        "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text); then
    echo "WARNING: failed to verify instance cleanup." >&2
    verified=0
elif [ -n "$LEFTOVERS" ] && [ "$LEFTOVERS" != "None" ]; then
    echo "WARNING: tagged instances still present: $LEFTOVERS" >&2
    echo "  manual cleanup:  aws ec2 terminate-instances --region $REGION --instance-ids $LEFTOVERS" >&2
    verified=0
fi

if [ "$SECURITY_GROUP_OWNED" = "1" ]; then
    if ! LEFTOVER_SG=$("${AWS_BASE[@]}" ec2 describe-security-groups \
        --filters "Name=group-name,Values=$SG_NAME" \
        --query 'SecurityGroups[].GroupId' --output text); then
        echo "WARNING: failed to verify security-group cleanup." >&2
        verified=0
    elif [ -n "$LEFTOVER_SG" ] && [ "$LEFTOVER_SG" != "None" ]; then
        echo "WARNING: security group $SG_NAME still exists: $LEFTOVER_SG" >&2
        echo "  manual cleanup:  aws ec2 delete-security-group --region $REGION --group-id $LEFTOVER_SG" >&2
        verified=0
    fi
fi

if [ "$KEY_PAIR_OWNED" = "1" ]; then
    if ! LEFTOVER_KP=$("${AWS_BASE[@]}" ec2 describe-key-pairs \
        --filters "Name=key-name,Values=$KEY_NAME" \
        --query 'KeyPairs[].KeyName' --output text); then
        echo "WARNING: failed to verify key-pair cleanup." >&2
        verified=0
    elif [ -n "$LEFTOVER_KP" ] && [ "$LEFTOVER_KP" != "None" ]; then
        echo "WARNING: key pair $KEY_NAME still exists in AWS." >&2
        verified=0
    fi
fi

if [ "$PLACEMENT_GROUP_OWNED" = "1" ]; then
    if ! LEFTOVER_PG=$("${AWS_BASE[@]}" ec2 describe-placement-groups \
        --filters "Name=group-name,Values=$PLACEMENT_GROUP_NAME" \
        --query 'PlacementGroups[].GroupName' --output text); then
        echo "WARNING: failed to verify placement-group cleanup." >&2
        verified=0
    elif [ -n "$LEFTOVER_PG" ] && [ "$LEFTOVER_PG" != "None" ]; then
        echo "WARNING: placement group $PLACEMENT_GROUP_NAME still exists." >&2
        echo "  manual cleanup:  aws ec2 delete-placement-group --region $REGION --group-name $PLACEMENT_GROUP_NAME" >&2
        verified=0
    fi
fi

if [ "$verified" -ne 1 ]; then
    echo "Cleanup is not verified; retaining $STATE_FILE for retry." >&2
    exit 1
fi

# 6. Remove local state and publish teardown proof -------------------------
if [ "$KEY_PAIR_OWNED" = "1" ] && [ -f "$LOCAL_KEY" ] && ! rm -f "$LOCAL_KEY"; then
    echo "Failed to remove the local key; retaining $STATE_FILE." >&2
    exit 1
fi
if ! rm -f "$AWS_DIR/.known_hosts"; then
    echo "Failed to remove known-host state; retaining $STATE_FILE." >&2
    exit 1
fi
if ! proof_tmp="$(mktemp "${TEARDOWN_PROOF}.tmp.XXXXXX")"; then
    echo "Failed to prepare teardown proof; retaining $STATE_FILE." >&2
    exit 1
fi
if ! printf 'verified\n' > "$proof_tmp"; then
    rm -f "$proof_tmp"
    echo "Failed to write teardown proof; retaining $STATE_FILE." >&2
    exit 1
fi
if ! mv "$proof_tmp" "$TEARDOWN_PROOF"; then
    rm -f "$proof_tmp"
    echo "Failed to publish teardown proof; retaining $STATE_FILE." >&2
    exit 1
fi
if ! rm -f "$STATE_FILE"; then
    rm -f "$TEARDOWN_PROOF"
    echo "Failed to remove $STATE_FILE; teardown proof was withdrawn." >&2
    exit 1
fi
echo "Removed local state."
echo "  clean — no resources created by this run remain."
