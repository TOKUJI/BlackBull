import boto3

iam = boto3.client("iam")

USER_NAME = "BlackBullDev"

# ---------------------------
# 1. ユーザー情報
# ---------------------------
print("### USER")
user = iam.get_user(UserName=USER_NAME)
print(user["User"]["UserName"])
print()

# ---------------------------
# 2. 所属グループ取得
# ---------------------------
print("### GROUPS")

groups_res = iam.list_groups_for_user(UserName=USER_NAME)
groups = groups_res.get("Groups", [])

group_names = [g["GroupName"] for g in groups]

print("Groups:", group_names)
print()

# ---------------------------
# 3. グループごとのポリシー展開
# ---------------------------
print("### GROUP POLICIES")

for group in groups:
    group_name = group["GroupName"]
    print(f"\n==== GROUP: {group_name} ====")

    # --- managed policies ---
    attached = iam.list_attached_group_policies(GroupName=group_name)
    print("  [Managed Policies]")
    for p in attached.get("AttachedPolicies", []):
        print("   -", p["PolicyName"], p["PolicyArn"])

    # --- inline policies ---
    inline = iam.list_group_policies(GroupName=group_name)
    print("  [Inline Policies]")
    for policy_name in inline.get("PolicyNames", []):
        policy = iam.get_group_policy(
            GroupName=group_name,
            PolicyName=policy_name
        )
        print("   -", policy_name)
        print("     PolicyDocument:", policy["PolicyDocument"])