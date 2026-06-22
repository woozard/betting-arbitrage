#!/bin/bash
# Open TCP 8080 on the EC2 security group for the monitor dashboard.
# Requires AWS CLI configured locally: aws configure
set -euo pipefail

INSTANCE_ID="${1:-i-0dd17929a8ebc3827}"
CIDR="${2:-0.0.0.0/0}"

SG_ID=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)

echo "Instance: $INSTANCE_ID"
echo "Security group: $SG_ID"
echo "Adding inbound TCP 8080 from $CIDR ..."

aws ec2 authorize-security-group-ingress \
  --group-id "$SG_ID" \
  --protocol tcp \
  --port 8080 \
  --cidr "$CIDR"

PUBLIC_IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

echo ""
echo "Done. Dashboard: http://${PUBLIC_IP}:8080/"
echo "Use MONITOR_TOKEN from server .env: grep MONITOR_TOKEN ~/betting-arbitrage/.env"
