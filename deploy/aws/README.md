# End-to-end AWS deployment guide

This guide deploys the project from Windows/VS Code using the included CloudFormation template and PowerShell script. The result is a React frontend and FastAPI API behind an Application Load Balancer, with separate ECS Fargate web, scheduler, and worker services; PostgreSQL state in RDS; images in ECR; a generated database password in Secrets Manager; and logs in CloudWatch.

The stack is a demo/portfolio deployment. The app has no user authentication or tenant isolation. Its starter network exposes the load balancer only to the CIDR you provide, and uses HTTP unless you configure TLS. Do not expose sensitive or personal data through it.

## 1. Prerequisites

- An AWS account in which you are permitted to create the resources listed below.
- AWS CLI v2, Docker Desktop, and PowerShell 7 (Windows PowerShell 5.1 may also work).
- The project ZIP extracted and opened at its root in VS Code. The root contains `deploy/aws/deploy.ps1`.
- An AWS region with at least two Availability Zones and the Fargate/RDS instance types used by the template available.
- A public IPv4 address from which you will access the site, written as a single-host CIDR, such as `198.51.100.24/32`. Use your own current public IP; the example address is reserved for documentation.

The deploy identity needs permissions for CloudFormation, EC2/VPC/networking, ECS, ECR, IAM role creation and `iam:PassRole`, Elastic Load Balancing, RDS, Secrets Manager, and CloudWatch Logs. Prefer an administrator-approved IAM Identity Center permission set or deployment role scoped for this stack. Do not put AWS access keys in `.env`, source files, or the ZIP.

The deployment creates billable resources, including an Application Load Balancer, RDS database, and continuously running Fargate tasks. Estimate costs for your region with the [AWS Pricing Calculator](https://calculator.aws/) and choose a teardown date before proceeding. This is not a free-tier-only deployment.

## 2. Configure and verify AWS access

In VS Code, open a PowerShell terminal. Confirm the tools are installed and Docker Desktop is running:

```powershell
aws --version
docker --version
docker compose version
```

For an organization using IAM Identity Center, configure a profile (the browser sign-in flow will open):

```powershell
aws configure sso --profile workflow-deploy
aws sso login --profile workflow-deploy
$env:AWS_PROFILE = 'workflow-deploy'
```

For an account that gave you another supported credential method, configure that method instead. Then verify the selected identity and region:

```powershell
aws sts get-caller-identity
$Region = 'us-east-1'
aws configure get region
```

Set `$Region` to your intended deployment region and use the same region for all following commands. The deploy script takes `-Region` explicitly, so it does not rely on the CLI default.

## 3. Prepare a TLS certificate (recommended)

For HTTPS with a custom domain, first own/control a domain and request or import a public ACM certificate in the **same region** as the stack. Validate the domain ownership and wait until ACM shows the certificate as `Issued`. The certificate must cover the hostname you plan to use. Use its ARN in the deployment command.

If you do not have a domain and certificate, you can deploy the temporary demo over HTTP by omitting `-CertificateArn`. HTTP is not encrypted in transit and the browser will show the load balancer hostname. Keep ingress restricted to your own IP and do not use real user data. Add DNS records only after deployment if using a custom domain; point the hostname to the load balancer DNS name printed by the script.

## 4. Deploy

In PowerShell, change to the project root if needed and confirm it:

```powershell
Get-Location
Test-Path .\deploy\aws\deploy.ps1
```

Run the script with your actual IP CIDR, region, and a short lowercase project name. The optional TLS line should only be used with a valid, issued ACM certificate ARN from this region:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\aws\deploy.ps1 `
  -Region $Region `
  -StackName workflow-engine `
  -ProjectName workflow-engine `
  -AllowedIngressCidr 'YOUR_PUBLIC_IP/32' `
  -CertificateArn 'arn:aws:acm:REGION:ACCOUNT_ID:certificate/CERTIFICATE_ID'
```

Replace both quoted placeholders before running. For HTTP-only testing, remove the `-CertificateArn ...` line. `AllowedIngressCidr` is mandatory; do not use `0.0.0.0/0` for this unauthenticated application. Project names must start with a letter and use only letters, digits, or hyphens (3–25 characters).

The script performs these steps:

1. Checks AWS credentials, validates the CloudFormation template, and creates the VPC, subnets, security groups, ECR repositories, ECS cluster/task definitions/services, load balancer, RDS database, secret, IAM roles, and log group with the services initially at zero tasks.
2. Builds the backend and frontend Docker images locally and pushes them to the stack's ECR repositories using a timestamped image tag.
3. Starts a one-off Fargate task to apply Alembic database migrations and checks its exit code.
4. Scales web, scheduler, and worker services to one task each, waits for ECS stability, and prints the application URL and image tag.

Initial deployment may take several minutes. Keep the terminal open. A failed update may leave services at zero tasks; resolve the reported issue and rerun the script. Redeploys intentionally scale services to zero while migrations run, so expect downtime. The RDS database and its data are retained on ordinary stack updates.

## 5. Verify the deployment

Open the `Deployment is ready` URL printed at the end. Then verify service health and the CloudFormation outputs:

```powershell
aws cloudformation describe-stacks `
  --stack-name workflow-engine `
  --region $Region `
  --query 'Stacks[0].{Status:StackStatus,Outputs:Outputs}'
aws ecs list-services --cluster workflow-engine-cluster --region $Region
```

If a custom domain is configured, create the DNS record that points that hostname to the `ApplicationUrl` load balancer hostname. On the site, open Workflow Builder, validate and save the starter workflow, then run it. Confirm the run appears in the control plane.

For diagnostics:

```powershell
aws ecs describe-services `
  --cluster workflow-engine-cluster `
  --services workflow-engine-web workflow-engine-scheduler workflow-engine-worker `
  --region $Region
aws logs tail /ecs/workflow-engine --follow --region $Region
```

The API, scheduler, worker, and migration containers write to this log group. ECS service events also explain failed health checks, image pulls, or task starts.

## 6. Deploy an improvement

Save your code changes, then from the project root rerun the same command and parameters:

```powershell
.\deploy\aws\deploy.ps1 `
  -Region $Region `
  -StackName workflow-engine `
  -ProjectName workflow-engine `
  -AllowedIngressCidr 'YOUR_PUBLIC_IP/32' `
  -CertificateArn 'arn:aws:acm:REGION:ACCOUNT_ID:certificate/CERTIFICATE_ID'
```

Use exactly the same stack/project name, ingress CIDR, and certificate settings as the original deployment. Each run tags and pushes new images, applies migrations, then starts the services. This script does not provide zero-downtime updates.

## 7. Restrict access and manage costs

- If your public IP changes, update the stack's `AllowedIngressCidr` to your new IP before you expect to connect. Use CloudFormation parameter update through the script with the new CIDR.
- Keep the website private to your IP range; this application does not have sign-in or authorization.
- Review CloudWatch logs and resource usage. The log group retains logs for 14 days.
- To stop compute charges temporarily while retaining the database and infrastructure, scale the three ECS services to zero in the ECS console or with `aws ecs update-service --desired-count 0` for each service. The load balancer and RDS continue to incur charges. Set desired count back to 1 to run again. The next script deployment also scales services as part of its migration sequence.

## 8. Delete the deployment

Export any workflow data you need before deleting. Deleting this stack removes the database and its data, database password secret, ECR repositories/images, load balancer, network, and services. Confirm the stack and region are correct, then:

```powershell
aws cloudformation delete-stack --stack-name workflow-engine --region $Region
aws cloudformation wait stack-delete-complete --stack-name workflow-engine --region $Region
```

If deletion fails, inspect the CloudFormation stack Events tab for the resource that could not be removed before retrying.

## Boundaries

This stack is a starter for demos and learning. It uses single-AZ RDS, one task for each service, public-addressed Fargate tasks with inbound access limited to the load balancer security group, and no managed application login. For production, add authentication and authorization, private task subnets with NAT or VPC endpoints, Multi-AZ RDS, deletion protection and backups, secret rotation, alarms, WAF/rate controls, and a rolling deployment design with backward-compatible expand/contract migrations. Review the [AWS CloudFormation template](workflow-engine.yml) before deploying and the [local VS Code guide](../../docs/vscode-local-setup.md) for local development.
