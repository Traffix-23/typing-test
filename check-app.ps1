# check-app.ps1
# Eddy's health check for the typing test app running in Docker.
# It makes sure Docker and the container are running, fixes them if not,
# checks the website answers, and writes the result to a log file.

$container = "typing-test"
$log = "C:\typing-test\health-log.txt"
$time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# 1. Make sure the Docker service is running
$docker = Get-Service docker
if ($docker.Status -ne "Running") {
    Start-Service docker
    Add-Content $log "$time Docker service was stopped, started it"
}

# 2. Make sure the container is running
$running = docker inspect -f "{{.State.Running}}" $container
if ($running -ne "true") {
    docker start $container
    Add-Content $log "$time Container was down, restarted it"
}

# 3. Check that the website answers
try {
    $page = Invoke-WebRequest -UseBasicParsing http://localhost -TimeoutSec 10
    $result = "$time Website OK (status $($page.StatusCode))"
}
catch {
    $result = "$time Website NOT responding"
}

Add-Content $log $result
Write-Output $result
