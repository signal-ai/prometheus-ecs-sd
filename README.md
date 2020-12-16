# prometheus-ecs-sd

ECS Service Discovery for Prometheus

## Info

This tool provides Prometheus service discovery for Docker containers running on AWS ECS. You can easily instrument your app using a Prometheus
client and enable discovery adding an ENV variable at the Service Task Definition. Your container will then be added
to the list of Prometheus targets to be scraped.

Bridge, host and awsvpc (EC2 and Fargate) network modes are supported.

Requires

-   `python3`
-   The `boto3` library
-   Prometheus `2.x`.

## Developing

Local development requires the [poetry](https://python-poetry.org/) tool, check the documentation for installation instructions.

To start the service run

```shell
AWS_PROFILE=<your_aws_profile> make dev-start
```

any AWS configuration supported by boto3 is supported, e.g. individual access/secret keys.

To format code run

```shell
make format
```

## Setup

`discoverecs.py` should run alongside the Prometheus server. It generates targets using JSON file service discovery. It can
be started by running:

```shell
python discoverecs.py --directory /opt/prometheus-ecs
```

note that the directory must already exist.

The output directory is then `/opt/prometheus-ecs` defined in your Prometheus config as a `file_sd_config` job:

```yaml
- job_name: 'ecs-1m'
  scrape_interval: 1m
  file_sd_configs:
      - files:
            - /opt/prometheus-ecs/1m-tasks.json
  relabel_configs:
      - source_labels: [metrics_path]
        action: replace
        target_label: __metrics_path__
        regex: (.+)
```

You can also specify a discovery interval with `--interval` (in seconds). Default is 60s. We also provide caching to minimize hitting query
rate limits with the AWS ECS API. `discoverecs.py` runs in a loop until interrupted and will output target information to stdout.

To make your application discoverable by Prometheus, you need to set the following environment variable in your task definition:

```json
{ "name": "PROMETHEUS", "value": "true" }
```

Metric path and scrape interval is supported via `PROMETHEUS_ENDPOINT`:

```text
"interval:/metric_path,..."
```

Examples:

```text
"5m:/mymetrics,30s:/mymetrics2"
"/mymetrics"
"30s:/mymetrics1,/mymetrics2"
```

Under ECS task definition (`task.json`):

```json
{ "name": "PROMETHEUS_ENDPOINT", "value": "5m:/mymetrics,30s:/mymetrics2" }
```

Available scrape intervals: `15s`, `30s`, `1m`, `5m`.

The default metric path is `/metrics`. The default scrape interval is `1m`.

The following Prometheus configuration should be used to support all available intervals:

```yaml
- job_name: 'ecs-15s'
  scrape_interval: 15s
  file_sd_configs:
      - files:
            - /opt/prometheus-ecs/15s-tasks.json
  relabel_configs:
      - source_labels: [metrics_path]
        action: replace
        target_label: __metrics_path__
        regex: (.+)

- job_name: 'ecs-30s'
  scrape_interval: 30s
  file_sd_configs:
      - files:
            - /opt/prometheus-ecs/30s-tasks.json
  relabel_configs:
      - source_labels: [metrics_path]
        action: replace
        target_label: __metrics_path__
        regex: (.+)

- job_name: 'ecs-1m'
  scrape_interval: 1m
  file_sd_configs:
      - files:
            - /opt/prometheus-ecs/1m-tasks.json
  relabel_configs:
      - source_labels: [metrics_path]
        action: replace
        target_label: __metrics_path__
        regex: (.+)

- job_name: 'ecs-5m'
  scrape_interval: 5m
  file_sd_configs:
      - files:
            - /opt/prometheus-ecs/5m-tasks.json
  relabel_configs:
      - source_labels: [metrics_path]
        action: replace
        target_label: __metrics_path__
        regex: (.+)
```

## EC2 IAM Policy

The following IAM Policy should be added when running `discoverecs.py` in EC2:

```JSON
  "Version": "2012-10-17",
  "Statement": [
    {
      "Action": ["ecs:Describe*", "ecs:List*"],
      "Effect": "Allow",
      "Resource": "*"
    }
```

You will also need EC2 Read Only Access. If you use Terraform:

```hcl
# Prometheus EC2 service discovery
resource "aws_iam_role_policy_attachment" "prometheus-server-role-ec2-read-only" {
  role = "${aws_iam_role.prometheus-server-role.name}"
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ReadOnlyAccess"
}
```

## Special cases

For skipping labels, set `PROMETHEUS_NOLABELS` to `true`.
This is useful when you use "blackbox" exporters or Pushgateway in a task
and metrics are exposed at a service level. This way, no EC2/ECS labels
will be exposed and the instance label will always point to the job name.

## Networking

All network modes are supported (`bridge`, `host` and `awsvpc`).

If `PROMETHEUS_PORT` and `PROMETHEUS_CONTAINER_PORT` are not set, the script will pick the first port from the container
definition (in `awsvpc` and `host` network mode) or the container host network bindings
in bridge mode. On Fargate, if `PROMETHEUS_PORT` is not set, it will default to port 80.

If `PROMETHEUS_CONTAINER_PORT` is set, it will look at the container host network bindings, and find the entry with a matching `containerPort`. It will then use the `hostPort` found there as target port.
This is useful when the container port is known, but the hostPort is randomly picked by ECS (by setting hostPort to 0 in the task definition).

If your container uses multiple ports, it's recommended to specify `PROMETHEUS_PORT` (`awsvpc`, `host`) or `PROMETHEUS_CONTAINER_PORT` (`bridge`).
