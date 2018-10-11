from __future__ import print_function
from collections import defaultdict
import boto3
import json
import argparse
import time
import os
import re

"""
Copyright 2018 Signal Media Ltd

ECS service discovery for tasks. Please enable it by setting env variable
PROMETHEUS to "true".

Metric path and scape interval is supported via PROMETHEUS_ENDPOINT:

"interval:/metric_path,..."

Examples:

"5m:/mymetrics,30s:/mymetrics2"
"/mymetrics"
"30s:/mymetrics1,/mymetrics2"

Under ECS task definition (task.json):

{"name": "PROMETHEUS_ENDPOINT", "value": "5m:/mymetrics,30s:/mymetrics2"}

Available intervals: 15s, 30s, 1m, 5m.

Default metric path is /metrics. Default interval is 1m.

For skipping labels, set PROMETHEUS_NOLABELS to "true".
This is useful when you use "blackbox" exporters or Pushgateway in a task
and metrics are exposed at a service level. This way, no ec2/ecs labels
will be exposed and the instance label will be empty.

PROMETHEUS_PORT can be used for tasks using classic ELB setup with multiple
port mappings.
"""

def log(message):
    print(message)

def chunk_list(l, n):
    return [l[i:i + n] for i in xrange(0, len(l), n)]

def dict_get(d, k, default):
    if k in d:
        return d[k]
    else:
        return default

class FlipCache():

    def __init__(self):
        self.current_cache = {}
        self.next_cache = {}
        self.hits = 0
        self.misses = 0

    def flip(self):
        self.current_cache = self.next_cache
        self.next_cache = {}
        self.hits = 0
        self.misses = 0

    def get_dict(self, keys, fetcher):
        missing = []
        result = {}
        for k in set(keys):
            if k in self.current_cache:
                result[k] = self.current_cache[k]
                self.hits += 1
            else:
                missing += [k]
                self.misses += 1
        fetched = fetcher(missing) if missing else {}
        result.update(fetched)
        self.current_cache.update(fetched)
        self.next_cache.update(result)
        return result

    def get(self, key, fetcher):
        if key in self.current_cache:
            result = self.current_cache[key]
            self.hits += 1
        else:
            self.misses += 1
            result = fetcher(key)
        if result:
            self.current_cache[key] = result
            self.next_cache[key] = result
        return result


class TaskInfo:

    def __init__(self, task):
        self.task = task
        self.task_definition = None
        self.container_instance = None
        self.ec2_instance = None

    def valid(self):
        return self.task_definition and self.container_instance and self.ec2_instance

class TaskInfoDiscoverer:

    def __init__(self):
        self.ec2_client = boto3.client('ec2')
        self.ecs_client = boto3.client('ecs')
        self.task_cache = FlipCache()
        self.task_definition_cache = FlipCache()
        self.container_instance_cache = FlipCache()
        self.ec2_instance_cache = FlipCache()

    def flip_caches(self):
        self.task_cache.flip()
        self.task_definition_cache.flip()
        self.container_instance_cache.flip()
        self.ec2_instance_cache.flip()

    def describe_tasks(self, cluster_arn, task_arns):
        def fetcher_task_definition(arn):
            return self.ecs_client.describe_task_definition(taskDefinition=arn)['taskDefinition']

        def fetcher(fetch_task_arns):
            tasks = {}
            result = self.ecs_client.describe_tasks(cluster=cluster_arn, tasks=fetch_task_arns)
            if 'tasks' in result:
                for task in result['tasks']:
                    no_network_binding = []
                    for container in task['containers']:
                        if 'networkBindings' not in container or len(container['networkBindings']) == 0:
                            no_network_binding.append(container['name'])
                    arn = task['taskDefinitionArn']
                    task_definition = self.task_definition_cache.get(arn, fetcher_task_definition)
                    if no_network_binding and task_definition.get('networkMode') != 'awsvpc':
                            no_cache = None
                            for container_definition in task_definition['containerDefinitions']:
                                prometheus = get_environment_var(container_definition['environment'], 'PROMETHEUS')
                                if container_definition['name'] in no_network_binding and prometheus:
                                    log(task['group'] + ':' + container_definition['name'] + ' does not have a networkBinding. Skipping for next run.')
                                    no_cache = True
                            if not no_cache:
                                tasks[task['taskArn']] = task
                    else:
                        tasks[task['taskArn']] = task
            return tasks
        return self.task_cache.get_dict(task_arns, fetcher).values()

    def create_task_infos(self, cluster_arn, task_arns):
        return map(lambda t: TaskInfo(t), self.describe_tasks(cluster_arn, task_arns))

    def add_task_definitions(self, task_infos):
        def fetcher(arn):
            return self.ecs_client.describe_task_definition(taskDefinition=arn)['taskDefinition']

        for task_info in task_infos:
            arn = task_info.task['taskDefinitionArn']
            task_info.task_definition = self.task_definition_cache.get(arn, fetcher)

    def add_container_instances(self, task_infos, cluster_arn):
        def fetcher(arns):
            arnsChunked = chunk_list(arns, 100)
            instances = {}
            for arns in arnsChunked:
                result = self.ecs_client.describe_container_instances(cluster=cluster_arn, containerInstances=arns)
                for i in dict_get(result, 'containerInstances', []):
                    instances[i['containerInstanceArn']] = i
            return instances

        containerInstanceArns = list(set(map(lambda t: t.task['containerInstanceArn'], task_infos)))
        containerInstances = self.container_instance_cache.get_dict(containerInstanceArns, fetcher)
        for t in task_infos:
            t.container_instance = dict_get(containerInstances, t.task['containerInstanceArn'], None)

    def add_ec2_instances(self, task_infos):
        def fetcher(ids):
            idsChunked = chunk_list(ids, 100)
            instances = {}
            for ids in idsChunked:
                result = self.ec2_client.describe_instances(InstanceIds=ids)
                for r in dict_get(result, 'Reservations', []):
                    for i in dict_get(r, 'Instances', []):
                        instances[i['InstanceId']] = i
            return instances

        instance_ids = list(set(map(lambda t: t.container_instance['ec2InstanceId'], task_infos)))
        instances = self.ec2_instance_cache.get_dict(instance_ids, fetcher)
        for t in task_infos:
            t.ec2_instance = dict_get(instances, t.container_instance['ec2InstanceId'], None)

    def get_infos_for_cluster(self, cluster_arn):
        tasks_pages = self.ecs_client.get_paginator('list_tasks').paginate(cluster=cluster_arn, launchType='EC2')
        task_infos = []
        for task_arns in tasks_pages:
            if task_arns['taskArns']:
                task_infos += self.create_task_infos(cluster_arn, task_arns['taskArns'])
        self.add_task_definitions(task_infos)
        self.add_container_instances(task_infos, cluster_arn)
        return task_infos

    def print_cache_stats(self):
        log('task_cache {} {} task_definition_cache {} {} {} container_instance_cache {} {} ec2_instance_cache {} {} {}'.format(
            self.task_cache.hits, self.task_cache.misses,
            self.task_definition_cache.hits, self.task_definition_cache.misses,
            len(self.task_definition_cache.current_cache),
            self.container_instance_cache.hits, self.container_instance_cache.misses,
            self.ec2_instance_cache.hits, self.ec2_instance_cache.misses,
            len(self.ec2_instance_cache.current_cache)))

    def get_infos(self):
        self.flip_caches()
        task_infos = []
        clusters_pages = self.ecs_client.get_paginator('list_clusters').paginate()
        for clusters in clusters_pages:
            for cluster_arn in clusters['clusterArns']:
                task_infos += self.get_infos_for_cluster(cluster_arn)
        self.add_ec2_instances(task_infos)
        self.print_cache_stats()
        return task_infos

class Target:

    def __init__(self, ip, port, metrics_path,
                 p_instance, ecs_task_id, ecs_task_name, ecs_task_version,
                 ecs_container_id, ecs_cluster_name, ec2_instance_id):
        self.ip = ip
        self.port = port
        self.metrics_path = metrics_path
        self.p_instance = p_instance
        self.ecs_task_id = ecs_task_id
        self.ecs_task_name = ecs_task_name
        self.ecs_task_version = ecs_task_version
        self.ecs_container_id = ecs_container_id
        self.ecs_cluster_name = ecs_cluster_name
        self.ec2_instance_id = ec2_instance_id

def get_environment_var(environment, name):
    for entry in environment:
        if entry['name'] == name:
            return entry['value']
    return None

def extract_name(arn):
    return arn.split(":")[5].split('/')[1]

def extract_task_version(taskDefinitionArn):
    return taskDefinitionArn.split(":")[6]

def extract_path_interval(env_variable):
    path_interval = {}
    if env_variable:
        for lst in env_variable.split(","):
            if ':' in lst:
                pi = lst.split(":")
                if re.search('(15s|30s|1m|5m)', pi[0]):
                    path_interval[pi[1]] = pi[0]
                else:
                    path_interval[pi[1]] = '1m'
            else:
                path_interval[lst] = '1m'
    else:
        path_interval['/metrics'] = '1m'
    return path_interval

def task_info_to_targets(task_info):
    if not task_info.valid():
        return []
    for container_definition in task_info.task_definition['containerDefinitions']:
        prometheus = get_environment_var(container_definition['environment'], 'PROMETHEUS')
        metrics_path = get_environment_var(container_definition['environment'], 'PROMETHEUS_ENDPOINT')
        nolabels = get_environment_var(container_definition['environment'], 'PROMETHEUS_NOLABELS')
        prom_port = get_environment_var(container_definition['environment'], 'PROMETHEUS_PORT')
        if nolabels != 'true': nolabels = None
        containers = filter(lambda c:c['name'] == container_definition['name'], task_info.task['containers'])
        if prometheus:
            for container in containers:
                ecs_task_name=extract_name(task_info.task['taskDefinitionArn'])
                if prom_port:
                    first_port = prom_port
                else:
                    first_port = str(container['networkBindings'][0]['hostPort'])
                if nolabels:
                    p_instance = ecs_task_id = ecs_task_version = ecs_container_id = ecs_cluster_name = ec2_instance_id = None
                else:
                    if task_info.task_definition.get('networkMode') == 'awsvpc':
                        eni = list(filter(lambda x: x.get('PrivateIpAddress') != task_info.ec2_instance['PrivateIpAddress'], task_info.ec2_instance.get('NetworkInterfaces')))[0]
                        interface_ip = eni.get('PrivateIpAddress')
                    else:
                        interface_ip = task_info.ec2_instance['PrivateIpAddress']
                    p_instance = interface_ip + ':' + first_port
                    ecs_task_id=extract_name(task_info.task['taskArn'])
                    ecs_task_version=extract_task_version(task_info.task['taskDefinitionArn'])
                    ecs_container_id=extract_name(container['containerArn'])
                    ecs_cluster_name=extract_name(task_info.task['clusterArn'])
                    ec2_instance_id=task_info.container_instance['ec2InstanceId']

                return [Target(
                    ip=interface_ip,
                    port=first_port,
                    metrics_path=metrics_path,
                    p_instance=p_instance,
                    ecs_task_id=ecs_task_id,
                    ecs_task_name=ecs_task_name,
                    ecs_task_version=ecs_task_version,
                    ecs_container_id=ecs_container_id,
                    ecs_cluster_name=ecs_cluster_name,
                    ec2_instance_id=ec2_instance_id)]
    return []

class Main:

    def __init__(self, directory, interval):
        self.directory = directory
        self.interval = interval
        self.discoverer = TaskInfoDiscoverer()

    def write_jobs(self, jobs):
        for i, j in jobs.items():
            file_name = self.directory + '/' + i + '-tasks.json'
            tmp_file_name = file_name + '.tmp'
            with open(tmp_file_name, 'w') as f:
                f.write(json.dumps(j, indent=4))
            os.rename(tmp_file_name, file_name)

    def get_targets(self):
        targets = []
        infos = self.discoverer.get_infos()
        for info in infos:
            targets += task_info_to_targets(info)
        return targets

    def discover_tasks(self):
        targets = self.get_targets()
        jobs = defaultdict(list)
        for i in ['15s','30s','1m','5m']:
            jobs[i] = []
        log('Targets: ' + str(len(targets)))
        for target in targets:
            path_interval = extract_path_interval(target.metrics_path)
            for path, interval in path_interval.items():
                labels = None
                if target.p_instance is not None:
                    labels = {
                        'instance': target.p_instance,
                        'ecs_task_id' : target.ecs_task_id,
                        'ecs_task_version' : target.ecs_task_version,
                        'ecs_container_id' : target.ecs_container_id,
                        'ecs_cluster' : target.ecs_cluster_name,
                        'instance_id' : target.ec2_instance_id
                    }
                job = {
                    'targets' : [target.ip + ':' + target.port],
                    'labels' : {
                        'job' : target.ecs_task_name,
                        'port' : target.port,
                        'metrics_path' : path
                    }
                }
                if labels:
                    job['labels'].update(labels)
                jobs[interval].append(job)
                log(job)
        self.write_jobs(jobs)

    def loop(self):
        while True:
            self.discover_tasks()
            time.sleep(self.interval)

def main():
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('--directory', required=True)
    arg_parser.add_argument('--interval', default=60)
    args = arg_parser.parse_args()
    log('Starting. Directory: ' + args.directory + '. Interval: ' + str(args.interval) + 's.')
    Main(args.directory, float(args.interval)).loop()

if __name__== "__main__":
    main()
