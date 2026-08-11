#!/usr/bin/env python3
"""
Digilux OTA — AWS CDK App (Python)

Usage:
  cdk deploy --all -c env=digilux          # deploy Digilux environment
  cdk deploy --all -c env=honeywell-prod   # deploy Honeywell production
  cdk diff  --all -c env=honeywell-prod    # preview changes before deploy
  cdk destroy --all -c env=honeywell-prod  # tear down (stateful resources retained)

Prerequisites (one-time, per environment):
  1. cdk bootstrap aws://ACCOUNT/REGION
  2. Create Secrets Manager secrets:
       {namePrefix}-ota-signing-key       — OTA package signing RSA private key (PEM)
       {namePrefix}-ota-cloudfront-key    — CloudFront RSA private key (PEM)
  3. Create CloudFront key pair and set cloudfrontKeyPairId in config
  4. Ensure deviceDataTableName table exists (managed by device platform team)
"""
import json
import os

import aws_cdk as cdk

from stacks.storage_stack import StorageStack
from stacks.iam_stack import IamStack
from stacks.lambda_stack import LambdaStack
from stacks.api_stack import ApiStack
from stacks.alarms_stack import AlarmsStack
from stacks.events_stack import EventsStack


def load_config(env_name: str) -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config", f"{env_name}.json")
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Config not found: {config_path}\n"
            f"Available environments: {', '.join(f.stem for f in (os.path.dirname(__file__) + '/config'))}"
        )
    with open(config_path) as f:
        return json.load(f)


app = cdk.App()
env_name = app.node.try_get_context("env") or "digilux"
config = load_config(env_name)

prefix = config["namePrefix"]
cdk_env = cdk.Environment(account=config["account"], region=config["region"])

storage = StorageStack(app, f"{prefix}-ota-storage", config=config, env=cdk_env)
iam = IamStack(app, f"{prefix}-ota-iam", config=config, storage=storage, env=cdk_env)
lambdas = LambdaStack(app, f"{prefix}-ota-lambda", config=config, storage=storage, iam_stack=iam, env=cdk_env)
events = EventsStack(app, f"{prefix}-ota-events", config=config, env=cdk_env)
api = ApiStack(app, f"{prefix}-ota-api", config=config, lambdas=lambdas, env=cdk_env)
alarms = AlarmsStack(app, f"{prefix}-ota-alarms", config=config, lambdas=lambdas, env=cdk_env)

# Explicit dependency ordering
iam.add_stack_dependency(storage)
lambdas.add_stack_dependency(iam)
events.add_stack_dependency(lambdas)
api.add_stack_dependency(lambdas)
alarms.add_stack_dependency(lambdas)

app.synth()
