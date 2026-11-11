#!/usr/bin/env python3
"""Read AWS stack outputs and generate nonsecret CFN inputs. Never deploys resources."""

import argparse
import ipaddress
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["settings", "rds", "iam", "eks", "identity", "nat"])
    parser.add_argument(
        "--config", type=Path, default=Path("infra/environments/seoul-lab.runtime.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--admin-cidr", help="Current developer public IPv4 /32; required for eks")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    base = [
        "aws",
        "--profile",
        config["profile"],
        "--region",
        config["region"],
        "--output",
        "json",
        "--no-cli-pager",
    ]

    def aws(*command):
        result = subprocess.run(base + list(command), text=True, capture_output=True, check=False)
        if result.returncode:
            # Only read-only API calls are used; do not continue with fabricated identifiers.
            raise SystemExit(f"AWS {' '.join(command[:2])} failed: {result.stderr.strip()}")
        return json.loads(result.stdout)

    identity = aws("sts", "get-caller-identity")
    if identity["Account"] != config["account_id"]:
        raise SystemExit("Account mismatch; parameter generation stopped")
    prefix = f"{config['project']}-{config['environment']}"

    def stack(name):
        value = aws("cloudformation", "describe-stacks", "--stack-name", name)["Stacks"][0]
        if value["StackStatus"] not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
            "UPDATE_ROLLBACK_COMPLETE",
        }:
            raise SystemExit(f"Stack {name} is not in a completed state: {value['StackStatus']}")
        return value

    def outputs(name):
        return {o["OutputKey"]: o["OutputValue"] for o in stack(name).get("Outputs", [])}

    foundation = outputs(config["foundation_stack"])
    public_ids = foundation["PublicSubnetIds"].split(",")
    app_ids = foundation["AppSubnetIds"].split(",")
    db_ids = foundation["DbSubnetIds"].split(",")
    all_ids = public_ids + app_ids + db_ids
    subnets = aws("ec2", "describe-subnets", "--subnet-ids", *all_ids)["Subnets"]
    by_id = {s["SubnetId"]: s for s in subnets}
    if (
        len(set(all_ids)) != 6
        or len(by_id) != 6
        or any(s["VpcId"] != foundation["VpcId"] or s["MapPublicIpOnLaunch"] for s in subnets)
    ):
        raise SystemExit("Foundation subnet identity/public-IP configuration mismatch")
    if any(
        len({by_id[s]["AvailabilityZone"] for s in pair}) != 2
        for pair in [public_ids, app_ids, db_ids]
    ):
        raise SystemExit("Each subnet pair must span two AZs")
    az_a = by_id[public_ids[0]]["AvailabilityZone"]
    app_a = next(s for s in app_ids if by_id[s]["AvailabilityZone"] == az_a)
    params = {"ProjectName": config["project"], "Environment": config["environment"]}

    if args.stage == "settings":
        params.update(VpcId=foundation["VpcId"], VpcCidr=foundation["VpcCidr"])
    elif args.stage == "nat":
        # Preserve every existing parameter and only enable the intended single NAT.
        params = {
            p["ParameterKey"]: p["ParameterValue"]
            for p in stack(config["foundation_stack"])["Parameters"]
        }
        params["NatMode"] = "single"
    else:
        settings = outputs(prefix + "-runtime-settings")
        if args.stage == "rds":
            params.update(
                VpcId=foundation["VpcId"],
                DbSubnetIds=",".join(db_ids),
                DbClientSecurityGroupId=settings["DbClientSecurityGroupId"],
                DbInstanceClass=config["db_instance_class"],
                EngineVersion=config["postgres_version"],
                AvailabilityZone=az_a,
                AllocatedStorage="20",
                MaxAllocatedStorage="100",
            )
        elif args.stage == "iam":
            database = outputs(prefix + "-rds-runtime")
            params.update(
                {
                    role + "SecretArn": settings[role + "SecretArn"]
                    for role in ["Api", "Collector", "Migration", "Import"]
                }
            )
            params.update(
                MasterSecretArn=database["MasterSecretArn"],
                DataBucketName=foundation["DataBucketName"],
                OperatorGroupName=config["operator_group"],
                BedrockProfileId=config["bedrock_profile_id"],
                BedrockModelId=config["bedrock_model_id"],
            )
        elif args.stage == "eks":
            try:
                network = ipaddress.ip_network(args.admin_cidr or "", strict=True)
            except ValueError:
                raise SystemExit("--admin-cidr must be a valid public IPv4 /32") from None
            if (
                network.version != 4
                or network.prefixlen != 32
                or not network.network_address.is_global
            ):
                raise SystemExit("--admin-cidr must be a single public IPv4 /32")
            params.update(
                AppSubnetIds=",".join(app_ids),
                WorkerSubnetId=app_a,
                DbClientSecurityGroupId=settings["DbClientSecurityGroupId"],
                ClusterRoleArn=config["cluster_role_arn"],
                NodeRoleArn=config["node_role_arn"],
                CniRoleArn=config["cni_role_arn"],
                AdminPrincipalArn=config["operator_arn"],
                AdminPublicCidr=str(network),
            )
            for key, source in {
                "KubernetesVersion": "kubernetes_version",
                "NodeInstanceType": "node_instance_type",
                "NodeReleaseVersion": "node_release_version",
                "VpcCniVersion": "vpc_cni_version",
                "CoreDnsVersion": "core_dns_version",
                "KubeProxyVersion": "kube_proxy_version",
                "PodIdentityAgentVersion": "pod_identity_agent_version",
            }.items():
                params[key] = config[source]
        elif args.stage == "identity":
            params.update(outputs(prefix + "-runtime-iam"))
            params["ClusterName"] = outputs(prefix + "-eks-runtime")["ClusterName"]

    # Reject an ARN for another account/region, even if a stack output was edited.
    for key, value in params.items():
        if "Arn" not in key:
            continue
        fields = value.split(":", 5)
        if (
            len(fields) != 6
            or fields[4] != config["account_id"]
            or fields[3] not in {"", config["region"]}
        ):
            raise SystemExit(f"Unexpected account or region in {key}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            [{"ParameterKey": k, "ParameterValue": str(v)} for k, v in params.items()], indent=2
        )
        + "\n"
    )
    print(
        f"Prepared {args.stage}: {len(params)} nonsecret parameters "
        f"for {config['account_id']}/{config['region']}"
    )


if __name__ == "__main__":
    main()
