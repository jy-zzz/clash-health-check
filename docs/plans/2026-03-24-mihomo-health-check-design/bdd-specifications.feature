# =============================================================================
# Trojan Proxy Health Monitoring System — BDD Specifications
# =============================================================================
# System: clash-health-check
# Components:
#   - Monitor  : runs on Ubuntu cron host, orchestrates Mihomo subprocesses
#   - Webhook  : runs on trojan proxy server as root, restarts trojan service
# Date: 2026-03-24
# =============================================================================


# =============================================================================
# FEATURE 1: Mihomo Subprocess Startup and Readiness Polling
# =============================================================================

Feature: Mihomo subprocess startup and readiness polling
  As the monitor process
  I want to start a Mihomo subprocess and confirm it is ready before proceeding
  So that health checks are issued against a fully initialized proxy runtime

  Background:
    Given the Mihomo binary is present at the configured path
    And a valid Mihomo configuration file exists at the configured path
    And the Mihomo REST API is configured to listen on a known host and port

  Scenario: Mihomo starts successfully and becomes ready within the timeout
    Given no Mihomo process is currently running
    When the monitor launches the Mihomo subprocess
    Then the Mihomo process is spawned as a child process
    And the monitor polls the Mihomo REST API readiness endpoint
    And the readiness endpoint returns HTTP 200 within the configured timeout
    And the monitor records that Mihomo is ready
    And health check execution proceeds

  Scenario: Mihomo readiness endpoint is initially unavailable but recovers before timeout
    Given the Mihomo process takes several seconds to bind its REST API port
    When the monitor polls the readiness endpoint
    Then the first several polling attempts receive a connection-refused error
    And the monitor waits the configured polling interval between each attempt
    And when the endpoint eventually returns HTTP 200 the monitor records readiness
    And health check execution proceeds

  Scenario: Mihomo fails to become ready within the configured timeout
    Given the Mihomo REST API never responds within the timeout window
    When the polling timeout elapses
    Then the monitor logs an error indicating Mihomo did not become ready
    And the monitor terminates the Mihomo subprocess
    And the monitor exits with a non-zero exit code
    And no health check requests are issued

  Scenario: Mihomo process exits unexpectedly before becoming ready
    Given the Mihomo process crashes immediately after being spawned
    When the monitor detects that the child process has terminated
    Then the monitor logs an error including the process exit code
    And the monitor does not wait for the readiness polling timeout to expire
    And the monitor exits with a non-zero exit code

  Scenario: Mihomo binary is not found at the configured path
    Given the Mihomo binary does not exist at the configured path
    When the monitor attempts to spawn the subprocess
    Then the monitor logs an error indicating the binary was not found
    And the monitor exits with a non-zero exit code
    And no subprocess is left running

  Scenario: Readiness polling respects the configured interval between attempts
    Given the readiness endpoint is not yet available
    When the monitor issues consecutive polling attempts
    Then each attempt is separated by exactly the configured polling interval
    And the total number of attempts does not exceed timeout divided by interval


# =============================================================================
# FEATURE 2: Health Check Triggering and Result Collection
# =============================================================================

Feature: Health check triggering and result collection via Mihomo REST API
  As the monitor process
  I want to trigger a health check for a configured proxy node through Mihomo
  So that I can collect the latency and availability data for that node

  Background:
    Given Mihomo is running and its REST API is available
    And the monitor is configured with a target proxy node name
    And the monitor is configured with a health check URL and timeout

  Scenario: Health check is triggered successfully for the configured node
    Given the configured proxy node exists in Mihomo's proxy list
    When the monitor sends a health check request to the Mihomo REST API for the node
    Then the request is a GET to the Mihomo health check endpoint for that node name
    And the request includes the configured test URL as a query parameter
    And the request includes the configured timeout as a query parameter

  Scenario: Mihomo returns a valid health check result with delay
    Given the health check request is sent successfully
    When the Mihomo REST API responds with HTTP 200
    And the response body contains an "alive" field set to true
    And the response body contains a "delay" field with a positive integer in milliseconds
    Then the monitor records the node as reachable
    And the monitor stores the returned delay value

  Scenario: Mihomo returns a result indicating the node is unreachable
    Given the health check request is sent successfully
    When the Mihomo REST API responds with HTTP 200
    And the response body contains an "alive" field set to false
    And the response body contains a "delay" field of zero
    Then the monitor records the node as unreachable
    And the monitor stores the zero delay value

  Scenario: Mihomo REST API returns an unexpected HTTP error during health check
    Given the health check request is sent
    When the Mihomo REST API responds with HTTP 500
    Then the monitor logs the unexpected HTTP status code
    And the monitor treats the node as unhealthy for evaluation purposes
    And processing continues to the evaluation and notification phase

  Scenario: Mihomo REST API is unreachable when the health check request is issued
    Given the Mihomo REST API does not respond within the request timeout
    When the HTTP request times out
    Then the monitor logs a connection or timeout error
    And the monitor treats the node as unhealthy for evaluation purposes

  Scenario: Health check response body is malformed or missing expected fields
    Given the Mihomo REST API responds with HTTP 200
    When the response body is not valid JSON or is missing "alive" and "delay" fields
    Then the monitor logs a parse error describing the malformed response
    And the monitor treats the node as unhealthy for evaluation purposes


# =============================================================================
# FEATURE 3: Node Health Evaluation
# =============================================================================

Feature: Node health evaluation by alive flag and delay threshold
  As the monitor process
  I want to evaluate collected health check data against defined criteria
  So that I can determine whether a proxy node requires a restart

  Background:
    Given a health check result has been collected for the configured node
    And the monitor is configured with a maximum acceptable delay threshold in milliseconds

  Scenario: Node is evaluated as healthy when alive and within delay threshold
    Given the health check result has "alive" set to true
    And the health check result has a delay of 150 ms
    And the configured delay threshold is 2000 ms
    When the monitor evaluates the node health
    Then the node is classified as healthy
    And no webhook notification is triggered

  Scenario: Node is evaluated as unhealthy when alive flag is false
    Given the health check result has "alive" set to false
    And the health check result has a delay of 0 ms
    When the monitor evaluates the node health
    Then the node is classified as unhealthy
    And the reason recorded is "node unreachable"

  Scenario: Node is evaluated as unhealthy when delay exceeds threshold
    Given the health check result has "alive" set to true
    And the health check result has a delay of 3500 ms
    And the configured delay threshold is 2000 ms
    When the monitor evaluates the node health
    Then the node is classified as unhealthy
    And the reason recorded includes the actual delay and the threshold value

  Scenario: Node is evaluated as unhealthy when delay equals threshold exactly
    Given the health check result has "alive" set to true
    And the health check result has a delay of 2000 ms
    And the configured delay threshold is 2000 ms
    When the monitor evaluates the node health
    Then the node is classified as unhealthy
    And the reason recorded includes the actual delay and the threshold value

  Scenario: Node is evaluated as healthy when delay is one millisecond below threshold
    Given the health check result has "alive" set to true
    And the health check result has a delay of 1999 ms
    And the configured delay threshold is 2000 ms
    When the monitor evaluates the node health
    Then the node is classified as healthy
    And no webhook notification is triggered

  Scenario: Node is evaluated as unhealthy when alive is false regardless of delay value
    Given the health check result has "alive" set to false
    And the health check result has a delay of 100 ms
    And the configured delay threshold is 2000 ms
    When the monitor evaluates the node health
    Then the node is classified as unhealthy
    And the reason recorded is "node unreachable"

  Scenario: Node is evaluated as unhealthy when the health check result could not be collected
    Given the health check data collection failed due to an API or network error
    When the monitor evaluates the node health
    Then the node is classified as unhealthy
    And the reason recorded references the collection failure


# =============================================================================
# FEATURE 4: Webhook Notification on Unhealthy Node
# =============================================================================

Feature: Webhook notification to trojan server on unhealthy node detection
  As the monitor process
  I want to POST to the trojan server's webhook endpoint when a node is unhealthy
  So that the trojan service is restarted automatically

  Background:
    Given a node has been classified as unhealthy
    And the monitor is configured with the webhook URL of the trojan server
    And the monitor is configured with the Bearer token for the webhook

  Scenario: Monitor sends a POST request to the webhook endpoint
    When the monitor dispatches the webhook notification
    Then a POST request is sent to the configured webhook URL
    And the request includes an "Authorization" header with value "Bearer <configured-token>"
    And the request is sent to the /restart path

  Scenario: Webhook endpoint responds with HTTP 200 indicating successful restart
    Given the webhook server accepts the request
    When the webhook server responds with HTTP 200
    Then the monitor logs a success message indicating the restart was triggered
    And the monitor records the HTTP 200 response status in its log

  Scenario: Webhook endpoint responds with HTTP 401 indicating bad credentials
    Given the webhook server rejects the request
    When the webhook server responds with HTTP 401
    Then the monitor logs an error indicating authentication failure
    And the monitor records the HTTP 401 response status in its log
    And the monitor does not retry the webhook request

  Scenario: Webhook endpoint responds with HTTP 500 indicating server error
    Given the webhook server encounters an internal error
    When the webhook server responds with HTTP 500
    Then the monitor logs an error with the HTTP 500 status
    And the monitor records the failure in its log

  Scenario: Webhook endpoint is unreachable due to network error
    Given the trojan server is not reachable from the monitor host
    When the POST request times out or receives a connection error
    Then the monitor logs a connectivity error describing the failure
    And the monitor does not crash

  Scenario: No webhook notification is sent when the node is healthy
    Given a node has been classified as healthy
    When the monitor completes its evaluation
    Then no HTTP request is sent to the webhook endpoint


# =============================================================================
# FEATURE 5: Mihomo Process Cleanup
# =============================================================================

Feature: Mihomo subprocess cleanup on normal exit and on error or signal
  As the monitor process
  I want to ensure the Mihomo subprocess is always terminated when the monitor exits
  So that no orphaned processes accumulate on the cron host

  Background:
    Given the Mihomo subprocess was started by the monitor

  Scenario: Mihomo is terminated after a successful health check run
    Given the health check and evaluation have completed normally
    When the monitor reaches its normal exit path
    Then the monitor sends a termination signal to the Mihomo subprocess
    And the monitor waits for the subprocess to exit
    And the monitor exits with exit code 0

  Scenario: Mihomo is terminated when health check collection fails
    Given an error occurs during health check collection
    When the monitor enters its error handling path
    Then the monitor sends a termination signal to the Mihomo subprocess
    And the monitor waits for the subprocess to exit
    And the monitor exits with a non-zero exit code

  Scenario: Mihomo is terminated when node evaluation triggers an unexpected exception
    Given an unhandled exception is raised during node evaluation
    When the monitor's top-level error handler catches the exception
    Then the monitor sends a termination signal to the Mihomo subprocess
    And the monitor logs the exception details
    And the monitor exits with a non-zero exit code

  Scenario: Mihomo is terminated when the monitor process receives SIGTERM
    Given the monitor process receives SIGTERM from the operating system
    When the monitor's signal handler is invoked
    Then the monitor sends a termination signal to the Mihomo subprocess
    And the monitor waits for the subprocess to exit
    And the monitor exits cleanly

  Scenario: Mihomo is terminated when the monitor process receives SIGINT
    Given the monitor process receives SIGINT
    When the monitor's signal handler is invoked
    Then the monitor sends a termination signal to the Mihomo subprocess
    And the monitor exits cleanly

  Scenario: Cleanup does not raise an error if the Mihomo subprocess already exited
    Given the Mihomo subprocess has already terminated on its own before cleanup
    When the monitor attempts to terminate the subprocess
    Then no error is raised
    And the monitor continues with its normal or error exit path

  Scenario: Cleanup is performed even when Mihomo startup itself failed
    Given the Mihomo subprocess failed to start or become ready
    When the monitor enters its error exit path
    Then the monitor attempts to terminate any partially started subprocess
    And the monitor exits with a non-zero exit code


# =============================================================================
# FEATURE 6: Cron Scheduling Behavior
# =============================================================================

Feature: Cron scheduling behavior of the monitor
  As the system operator
  I want the monitor to execute periodically via cron
  So that node health is checked on a regular schedule without manual intervention

  Background:
    Given the monitor script is registered as a cron job on the Ubuntu host
    And the cron schedule is configured to the desired execution frequency

  Scenario: Monitor runs and exits within a single cron interval
    Given the cron interval is set to 5 minutes
    When the cron job triggers the monitor
    Then the monitor starts a Mihomo subprocess
    And the monitor completes the full health check and evaluation cycle
    And the monitor terminates the Mihomo subprocess
    And the monitor exits before the next cron interval begins

  Scenario: Each cron invocation is a fully independent and ephemeral run
    Given the previous cron invocation completed successfully
    When the next cron interval triggers a new monitor invocation
    Then the new invocation starts its own Mihomo subprocess from scratch
    And it does not share state or processes with the previous invocation

  Scenario: A failed previous run does not block the next scheduled run
    Given the previous cron invocation exited with a non-zero exit code
    When the next cron interval triggers a new monitor invocation
    Then the new invocation starts normally
    And the failure of the previous run has no effect on the new run

  Scenario: Cron captures monitor stdout and stderr to a log file
    Given the cron job is configured to redirect output to a log file
    When the monitor runs and produces log output
    Then all stdout and stderr lines are appended to the configured log file
    And each log line is available for inspection after the run

  Scenario: No Mihomo processes accumulate across multiple cron runs
    Given 10 consecutive cron invocations have completed
    When the system process list is inspected
    Then no residual Mihomo processes from previous runs are present

  Scenario: Overlapping cron invocations do not occur under normal conditions
    Given the monitor completes well within the cron interval
    When the cron scheduler fires the next invocation
    Then the previous invocation has already exited
    And there is only one monitor process running at a time


# =============================================================================
# FEATURE 7: Webhook Server Authentication
# =============================================================================

Feature: Webhook server authentication via Bearer token
  As the webhook server
  I want to validate the Authorization header on every POST /restart request
  So that only authorized callers can trigger a trojan service restart

  Background:
    Given the webhook server is running and listening on the configured port
    And the server is configured with a secret Bearer token

  Scenario: Request with valid Bearer token is accepted
    Given a POST request to /restart
    And the request carries the header "Authorization: Bearer <valid-token>"
    When the server processes the request
    Then the server verifies the token matches the configured secret
    And the server proceeds to execute the restart command

  Scenario: Request missing the Authorization header is rejected
    Given a POST request to /restart
    And the request has no "Authorization" header
    When the server processes the request
    Then the server returns HTTP 401
    And the response body indicates missing authorization
    And no systemctl command is executed

  Scenario: Request with Authorization header but no Bearer prefix is rejected
    Given a POST request to /restart
    And the request carries the header "Authorization: <valid-token>" without the Bearer scheme
    When the server processes the request
    Then the server returns HTTP 401
    And no systemctl command is executed

  Scenario: Request with wrong token value is rejected
    Given a POST request to /restart
    And the request carries the header "Authorization: Bearer wrong-token"
    When the server processes the request
    Then the server returns HTTP 401
    And the response body indicates authorization failure
    And no systemctl command is executed

  Scenario: Request with empty token value is rejected
    Given a POST request to /restart
    And the request carries the header "Authorization: Bearer "
    When the server processes the request
    Then the server returns HTTP 401
    And no systemctl command is executed

  Scenario: Authentication check is case-sensitive for the token value
    Given a POST request to /restart
    And the configured token is "SecretToken123"
    And the request carries the header "Authorization: Bearer secrettoken123"
    When the server processes the request
    Then the server returns HTTP 401
    And no systemctl command is executed

  Scenario: Authentication check treats the "Bearer " scheme prefix as case-insensitive
    Given a POST request to /restart
    And the request carries the header "Authorization: bearer <valid-token>"
    When the server processes the request
    Then the server accepts the request and proceeds


# =============================================================================
# FEATURE 8: Webhook Server — Successful Service Restart
# =============================================================================

Feature: Webhook server executes trojan service restart on authenticated request
  As the webhook server
  I want to run "systemctl restart trojan" when I receive a valid authenticated request
  So that the trojan proxy service is restarted promptly

  Background:
    Given the webhook server is running as root
    And the server is configured with a valid Bearer token
    And a POST request to /restart arrives with the correct Authorization header

  Scenario: systemctl restart trojan completes successfully
    Given the systemctl command exits with code 0
    When the server executes "systemctl restart trojan"
    Then the server returns HTTP 200
    And the response body confirms the restart was triggered
    And the server logs the restart action with a timestamp

  Scenario: Response is returned only after systemctl completes
    Given the systemctl command takes 3 seconds to complete
    When the server executes the restart command
    Then the HTTP response is not sent until the command has finished
    And the response reflects the final exit code of the command

  Scenario: Server remains available for subsequent requests after a successful restart
    Given a successful restart request was processed
    When a new POST request to /restart arrives with a valid token
    Then the server processes the new request normally
    And the server is not in a broken or degraded state

  Scenario: Server logs include the source IP of the requesting client
    Given a valid POST request arrives from IP address 10.0.0.5
    When the server processes the request
    Then the server log entry for this restart includes the client IP 10.0.0.5


# =============================================================================
# FEATURE 9: Webhook Server — Error Handling
# =============================================================================

Feature: Webhook server error handling for systemctl failures and unexpected conditions
  As the webhook server
  I want to handle errors gracefully and return appropriate HTTP status codes
  So that callers can determine whether the restart succeeded or failed

  Background:
    Given the webhook server is running
    And a POST request to /restart arrives with a valid Authorization header

  Scenario: systemctl restart trojan exits with a non-zero exit code
    Given the systemctl command exits with code 1
    And the command writes an error message to stderr
    When the server attempts to execute "systemctl restart trojan"
    Then the server returns HTTP 500
    And the response body includes an error description
    And the server logs the systemctl exit code and stderr output

  Scenario: systemctl binary is not found on the system
    Given the systemctl binary is not present on the system PATH
    When the server attempts to execute the restart command
    Then the server returns HTTP 500
    And the server logs the missing binary error

  Scenario: Execution of systemctl times out
    Given the systemctl command does not complete within the configured execution timeout
    When the execution timeout elapses
    Then the server kills the systemctl process
    And the server returns HTTP 500
    And the response body indicates a timeout occurred
    And the server logs the timeout event

  Scenario: Request arrives on an unsupported HTTP method
    Given a GET request arrives at /restart
    When the server processes the request
    Then the server returns HTTP 405 Method Not Allowed
    And no systemctl command is executed

  Scenario: Request arrives at an unknown path
    Given a POST request arrives at /unknown-path
    When the server processes the request
    Then the server returns HTTP 404 Not Found
    And no systemctl command is executed

  Scenario: Server handles concurrent requests without corruption
    Given two POST requests with valid tokens arrive simultaneously
    When both requests are processed
    Then each request receives an independent HTTP response
    And each systemctl invocation is logged separately

  Scenario: Server continues running after a systemctl failure
    Given a previous request caused systemctl to exit with an error
    When a new valid POST request to /restart arrives
    Then the server processes the new request normally
    And the server has not crashed or stopped listening


# =============================================================================
# FEATURE 10: Monitor Log Output Format and Content
# =============================================================================

Feature: Monitor log output format and content
  As the system operator
  I want the monitor to produce structured and timestamped log output
  So that I can audit health check history and diagnose failures from cron logs

  Background:
    Given the monitor has been invoked by the cron scheduler

  Scenario: Log entry includes a timestamp on every line
    When the monitor produces any log output
    Then every log line begins with a timestamp in ISO 8601 format
    And the timestamp includes at least the date, hour, minute, and second

  Scenario: Log records Mihomo startup event
    When the monitor spawns the Mihomo subprocess
    Then a log entry is written indicating the subprocess was started
    And the log entry includes the Mihomo process identifier

  Scenario: Log records Mihomo readiness event
    When Mihomo becomes ready and the readiness endpoint returns HTTP 200
    Then a log entry is written indicating Mihomo is ready
    And the log entry includes the elapsed time until readiness in milliseconds

  Scenario: Log records the health check request being sent
    When the monitor sends a health check request to the Mihomo REST API
    Then a log entry is written indicating the request was dispatched
    And the log entry includes the target node name and the test URL

  Scenario: Log records health check result with delay and alive status
    When the monitor receives a health check response from Mihomo
    Then a log entry is written with the node name, alive flag, and delay value

  Scenario: Log records healthy node classification
    Given the node was classified as healthy
    When the monitor completes evaluation
    Then a log entry is written stating the node is healthy
    And the log entry includes the measured delay value

  Scenario: Log records unhealthy node classification with reason
    Given the node was classified as unhealthy
    When the monitor completes evaluation
    Then a log entry is written stating the node is unhealthy
    And the log entry includes the reason for the unhealthy classification
    And if the reason is high delay the log includes both the measured delay and the threshold

  Scenario: Log records webhook notification dispatch
    Given the node was classified as unhealthy
    When the monitor sends the webhook POST request
    Then a log entry is written indicating the webhook notification was sent
    And the log entry includes the webhook URL (excluding the token value)

  Scenario: Log records webhook notification outcome
    When the webhook server responds to the POST request
    Then a log entry is written with the HTTP status code returned by the webhook server

  Scenario: Log records Mihomo subprocess termination
    When the monitor terminates the Mihomo subprocess
    Then a log entry is written indicating the subprocess was terminated

  Scenario: Log records an error when Mihomo fails to become ready
    Given Mihomo did not become ready within the timeout
    When the monitor logs the failure
    Then the log entry has an ERROR severity indicator
    And the log entry describes the timeout condition

  Scenario: Log records an error when webhook notification fails
    Given the webhook endpoint returned a non-200 status or was unreachable
    When the monitor logs the failure
    Then the log entry has an ERROR severity indicator
    And the log entry includes the failure details
    And the token value is never written to the log in plaintext

  Scenario: Log does not expose the Bearer token in any output
    Given the monitor is configured with a sensitive Bearer token
    When any log output is produced throughout the full execution cycle
    Then no log line contains the literal Bearer token value
