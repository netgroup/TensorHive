from tensorhive.core.monitors.Monitor import Monitor
from tensorhive.core.utils.decorators import override
import logging
log = logging.getLogger(__name__)


class CPUMonitor(Monitor):

    @override
    def update(self, group_connection, infrastructure_manager):
        cmd = 'awk \'{u=$2+$4; t=$2+$4+$5; if (NR==1){u1=u; t1=t;} else print ($2+$4-u1) * 100 / (t-t1); }\' \
        <(grep \'cpu \' /proc/stat) <(sleep 1;grep \'cpu \' /proc/stat);free -m | awk \'NR==2\''
        output = group_connection.run_command(cmd, stop_on_errors=False)
        group_connection.join(output)

        #for host, host_out in output.items():
        for host_output in output:
            uuid = 'CPU_{}'.format(host_output.host)
            metrics = {uuid: {'index': 0, 'metrics': dict()}}
            if host_output.exit_code == 0:
                # Command executed successfully
                stdout_lines = list(host_output.stdout)
                assert stdout_lines, 'stdout is empty!'
                stdout_lines[0] = stdout_lines[0].replace(',', '.')
                metrics[uuid]['metrics']['utilization'] = {'unit': '%', 'value': float(stdout_lines[0])}
                mem = stdout_lines[1].split()
                metrics[uuid]['metrics']['mem_total'] = {'unit': 'MiB', 'value': int(mem[1])}
                metrics[uuid]['metrics']['mem_used'] = {'unit': 'MiB', 'value': int(mem[2])}
                metrics[uuid]['metrics']['mem_free'] = {'unit': 'MiB', 'value': int(mem[3])}
            else:
                # Command execution failed
                if host_output.exit_code:
                    log.error('cpu query failed with {} exit code on {}'.format(host_output.exit_code, host_output.host))
                elif host_output.exception:
                    log.error('cpu query raised {} on {}'.format(host_output.exception.__class__.__name__, host_output.host))
                metrics = None
            infrastructure_manager.infrastructure[host_output.host]['CPU'] = metrics
