[CmdletBinding()]
param(
    [string]$Region = 'us-east-1',
    [string]$StackName = 'workflow-engine',
    [string]$ProjectName = 'workflow-engine',
    [Parameter(Mandatory = $true)]
    [string]$AllowedIngressCidr,
    [string]$CertificateArn = '',
    [string]$DatabaseInstanceClass = 'db.t4g.micro',
    [string]$ImageTag = (Get-Date -Format 'yyyyMMddHHmmss')
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$template = Join-Path $PSScriptRoot 'workflow-engine.yml'

foreach ($tool in @('aws', 'docker')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Required tool '$tool' was not found. Install AWS CLI v2 and Docker Desktop, then retry."
    }
}

aws sts get-caller-identity --region $Region | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'AWS credentials are not configured or lack access.' }

$commonParameters = @(
    "ProjectName=$ProjectName",
    "AllowedIngressCidr=$AllowedIngressCidr",
    "ImageTag=$ImageTag",
    "CertificateArn=$CertificateArn",
    "DatabaseInstanceClass=$DatabaseInstanceClass"
)

function Deploy-Stack([int]$Count) {
    aws cloudformation deploy `
        --template-file $template `
        --stack-name $StackName `
        --capabilities CAPABILITY_IAM `
        --no-fail-on-empty-changeset `
        --parameter-overrides @commonParameters "DesiredCount=$Count" `
        --region $Region
    if ($LASTEXITCODE -ne 0) { throw "CloudFormation deployment failed (DesiredCount=$Count)." }
}

function Get-StackOutputs {
    $stack = aws cloudformation describe-stacks --stack-name $StackName --region $Region --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read CloudFormation outputs.' }
    $values = @{}
    foreach ($item in $stack.Stacks[0].Outputs) { $values[$item.OutputKey] = $item.OutputValue }
    return $values
}

Push-Location $repo
try {
    aws cloudformation validate-template --template-body file://deploy/aws/workflow-engine.yml --region $Region | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'AWS rejected the CloudFormation template. Review the reported validation error.' }

    Write-Host "Preparing AWS resources with services scaled to zero (image tag $ImageTag)..."
    Deploy-Stack 0
    $outputs = Get-StackOutputs
    $backendUri = $outputs.BackendRepositoryUri
    $frontendUri = $outputs.FrontendRepositoryUri
    $registry = ($backendUri -split '/')[0]

    aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $registry
    if ($LASTEXITCODE -ne 0) { throw 'ECR login failed.' }

    docker build --tag "${backendUri}:${ImageTag}" --file Dockerfile .
    if ($LASTEXITCODE -ne 0) { throw 'Backend image build failed.' }
    docker build --tag "${frontendUri}:${ImageTag}" --file frontend/Dockerfile frontend
    if ($LASTEXITCODE -ne 0) { throw 'Frontend image build failed.' }
    docker push "${backendUri}:${ImageTag}"
    if ($LASTEXITCODE -ne 0) { throw 'Backend image push failed.' }
    docker push "${frontendUri}:${ImageTag}"
    if ($LASTEXITCODE -ne 0) { throw 'Frontend image push failed.' }

    Write-Host 'Applying database migrations in a one-off Fargate task...'
    $network = $outputs.MigrationNetworkConfiguration
    $migrationTask = aws ecs run-task `
        --cluster $outputs.ClusterName `
        --task-definition $outputs.MigrationTaskDefinitionArn `
        --launch-type FARGATE `
        --network-configuration $network `
        --region $Region `
        --query 'tasks[0].taskArn' --output text
    if ($LASTEXITCODE -ne 0 -or -not $migrationTask -or $migrationTask -eq 'None') {
        throw 'Could not start the database migration task.'
    }
    aws ecs wait tasks-stopped --cluster $outputs.ClusterName --tasks $migrationTask --region $Region
    if ($LASTEXITCODE -ne 0) { throw "Migration task did not stop cleanly: $migrationTask" }
    $exitCode = aws ecs describe-tasks --cluster $outputs.ClusterName --tasks $migrationTask --region $Region `
        --query "tasks[0].containers[?name=='migrate'].exitCode | [0]" --output text
    if ($LASTEXITCODE -ne 0 -or $exitCode -ne '0') {
        throw "Database migration failed (container exit code: $exitCode). Inspect the migration log stream in CloudWatch Logs."
    }

    Write-Host 'Starting the web, scheduler, and worker services...'
    Deploy-Stack 1
    aws ecs wait services-stable --cluster $outputs.ClusterName `
        --services "$ProjectName-web" "$ProjectName-scheduler" "$ProjectName-worker" --region $Region
    if ($LASTEXITCODE -ne 0) { throw 'ECS services did not become stable. Inspect ECS service events and CloudWatch logs.' }
    $outputs = Get-StackOutputs
    Write-Host "Deployment is ready: $($outputs.ApplicationUrl)"
    Write-Host "Image tag: $ImageTag"
} finally {
    Pop-Location
}
