#!/usr/bin/env python

"""
analyze_hosts - scans one or more hosts for security misconfigurations

Copyright (C) 2015-2016 Peter Mosmans [Go Forward]
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import os
import signal
import ssl
import subprocess
import sys
import tempfile
import textwrap
import time
import urlparse
try:
    import nmap
except ImportError:
    print('Please install python-nmap, e.g. pip install python-nmap',
          file=sys.stderr)
    sys.exit(-1)
try:
    import requests
    import Wappalyzer
except ImportError:
    print('[-] Please install the modules in requirements.txt, e.g. '
          'pip install -r requirements.txt')


VERSION = '0.13'
ALLPORTS = [25, 80, 443, 465, 993, 995, 8080]
SCRIPTS = """banner,dns-nsid,dns-recursion,http-cisco-anyconnect,\
http-php-version,http-title,http-trace,ntp-info,ntp-monlist,nbstat,\
rdp-enum-encryption,rpcinfo,sip-methods,smb-os-discovery,smb-security-mode,\
smtp-open-relay,ssh2-enum-algos,vnc-info,xmlrpc-methods,xmpp-info"""
UNKNOWN = -1


def analyze_url(url, port, options):
    """
    Analyzes an URL using wappalyzer and executes corresponding scans.
    """
    if options['framework']:
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning)
        if not urlparse.urlparse(url).scheme:
            if port == 443:
                url = 'https://{0}:{1}'.format(url, port)
            else:
                url = 'http://{0}:{1}'.format(url, port)
        wappalyzer = Wappalyzer.Wappalyzer.latest()
        try:
            page = requests.get(url, auth=None, proxies={}, verify=False)
            if page.status_code == 200:
                webpage = Wappalyzer.WebPage(url, page.text, page.headers)
                analysis = wappalyzer.analyze(webpage)
                print_status('Analysis for {0}: {1}'.format(url, analysis),
                             options)
                append_logs(options,
                            'Analysis for {0}: {1}'.format(url, analysis))
                if 'Drupal' in analysis:
                    do_droopescan(url, 'drupal', options)
                if 'Joomla' in analysis:
                    do_droopescan(url, 'joomla', options)
                if 'WordPress' in analysis:
                    do_wpscan(url, options)
            else:
                print_error('Got result {0} - cannot analyze that...'.
                            format(page.status_code))
        except requests.exceptions.ConnectionError as exception:
            print_error('Could not connect to {0} ({1})'.
                        format(url, exception))


def is_admin():
    """
    Returns true if script is executed using root privileges
    """
    if os.name == 'nt':
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin()
        except ImportError:
            return False
    else:
        return os.geteuid() == 0  # pylint: disable=no-member


def timestamp():
    """
    Returns timestamp.
    """
    return time.strftime("%H:%M:%S %d-%m-%Y")


def exit_gracefully(signum, frame):
    """
    Handle interrupts gracefully.
    """
    global child
    signal.signal(signal.SIGINT, original_sigint)
    try:
        if len(child):
            if raw_input('\nKill running process {0} ? (y/n) '.
                         format(child[1])).lower().startswith('y'):
                os.kill(child[0], signal.SIGHUP)
        if raw_input("\nQuit analyze_hosts ? (y/n) ").lower().startswith('y'):
            print_error('Quitting...', -1)
    except KeyboardInterrupt:
        print_error('Quitting...', -1)
    signal.signal(signal.SIGINT, [], exit_gracefully)


def print_error(text, result=False):
    """
    Prints error message
    When @result, exits with result.
    """
    if len(text):
        print_line('[-] ' + text, True)
    if result:
        sys.exit(result)


def print_line(text, error=False):
    """
    Prints text, and flushes stdout and stdin.
    When @error, prints text to stderr instead of stdout.
    """
    if not error:
        print(text)
    else:
        print(text, file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()


def print_status(text, options=False):
    """
    Prints status message if options array is given and contains 'verbose'.
    """
    if options and options['verbose']:
        print_line('[*] ' + text)


def preflight_checks(options):
    """
    Checks if all tools are there, and disables tools automatically.
    """
    if options['resume']:
        if not os.path.isfile(options['queuefile']) or \
           not os.stat(options['queuefile']).st_size:
            print_error('Cannot resume - queuefile {0} is empty'.
                        format(options['queuefile']), True)
    else:
        if os.path.isfile(options['queuefile']) and \
           os.stat(options['queuefile']).st_size:
            print_error('WARNING: Queuefile {0} already exists.\n'.
                        format(options['queuefile']) +
                        '    Use --resume to resume with previous targets, ' +
                        'or delete file manually', True)
    for basic in ['nmap']:
        options[basic] = True
    if options['udp'] and not is_admin():
        print_error('UDP portscan needs root permissions', True)
    try:
        import requests
        import Wappalyzer
    except ImportError:
        print_error('Disabling --framework due to missing Python libraries')
        options['framework'] = False
    if options['framework']:
        options['droopescan'] = True
        options['wpscan'] = True
    if options['wpscan'] and not is_admin():
        print_error('Disabling --wpscan as this option needs root permissions')
        options['wpscan'] = False
    options['timeout'] = options['testssl.sh']
    for tool in ['curl', 'droopescan', 'nikto', 'nmap', 'testssl.sh',
                 'timeout', 'wpscan']:
        if options[tool]:
            print_status('Checking whether {0} is present... '.
                         format(tool), options)
            result, _stdout, _stderr = execute_command([tool, '--help'],
                                                       options)  # pylint: disable=unused-variable
            if not result:
                print_error('FAILED: Could not execute {0}, disabling checks'.
                            format(tool), False)
                options[tool] = False


def execute_command(cmd, options):
    """
    Executes command.

    Returns result, stdout, stderr
    """
    stdout = ''
    stderr = ''
    result = False
    global child
    child = []
    if options['dry_run']:
        print_line(' '.join(cmd))
        return True, stdout, stderr
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        child.append(process.pid)
        child.append(cmd[0])
        stdout, stderr = process.communicate()
        result = not process.returncode
    except OSError:
        pass
    child = []
    return result, stdout, stderr


def download_cert(host, port, options):
    """
    Downloads an SSL certificate and appends it to the logfile.
    """
    if options['sslcert']:
        try:
            cert = ssl.get_server_certificate((host, port))
            append_logs(options, cert)
        except ssl.SSLError:
            pass


def append_logs(options, stdout, stderr=None):
    """
    Append text strings to logfile.
    """
    if options['dry_run']:
        return
    try:
        if stdout:
            with open(options['output_file'], 'a+') as open_file:
                open_file.write(compact_strings(stdout, options))
        if stderr:
            with open(options['output_file'], 'a+') as open_file:
                open_file.write(compact_strings(stderr, options))
    except IOError:
        print_error('FAILED: Could not write to {0}'.
                    format(options['output_file']), -1)


def append_file(options, input_file):
    """
    Append file to logfile, and deletes @input_file.
    """
    if options['dry_run']:
        return
    try:
        if os.path.isfile(input_file) and os.stat(input_file).st_size:
            with open(input_file, 'r') as read_file:
                append_logs(options, read_file.read())
    except IOError as exception:
        print_error('FAILED: Could not read {0} ({1}'.
                    format(input_file, exception), -1)


def compact_strings(strings, options):
    """
    Removes as much unnecessary strings as possible.
    """
    # remove ' (OK)'
    # remove ^SF:
    # remove
    if not options['compact']:
        return strings
    return '\n'.join([x for x in strings.splitlines() if x and
                      not x.startswith('#')]) + '\n'


def do_curl(host, port, options):
    """
    Checks for HTTP TRACE method.
    """
    if options['trace']:
        command = ['curl', '-qsIA', "'{0}'".format(options['header']),
                   '--connect-timeout', str(options['timeout']), '-X', 'TRACE',
                   '{0}:{1}'.format(host, port)]
        _result, stdout, stderr = execute_command(command, options)  # pylint: disable=unused-variable
        append_logs(options, stdout, stderr)


def do_droopescan(url, cms, options):
    """
    Performs a droopescan of type @cmd
    """
    if options['droopescan']:
        print_status('Performing droopescan on {0} of type {1}'.format(url,
                                                                       cms),
                     options)
        command = ['droopescan', 'scan', cms, '--quiet', '--url', url]
        _result, stdout, stderr = execute_command(command, options)  # pylint: disable=unused-variable
        append_logs(options, stdout, stderr)


def do_nikto(host, port, options):
    """
    Performs a nikto scan.
    """
    command = ['nikto', '-vhost', '{0}'.format(host), '-maxtime',
               '{0}s'.format(options['maxtime']), '-host',
               '{0}:{1}'.format(host, port)]
    if port == 443:
        command.append('-ssl')
    _result, stdout, stderr = execute_command(command, options)  # pylint: disable=unused-variable
    append_logs(options, stdout, stderr)


def do_portscan(host, options):
    """
    Performs a portscan.


    Returns:
        A list of open ports.

    Arguments:
        host: target host in string
        options: dictionary with options
    """
    if not options['nmap']:
        return ALLPORTS
    open_ports = []
    arguments = '--open'
    if is_admin():
        arguments += ' -sS'
        if options['udp']:
            arguments += ' -sU'
    else:
        arguments += ' -sT'
    if options['port']:
        arguments += ' -p' + options['port']
    if options['no_portscan']:
        arguments = '-sn -Pn'
    arguments += ' -sV --script=' + SCRIPTS
    if options['whois']:
        arguments += ',whois-domain,whois-ip,fcrdns'
    if options['allports']:
        arguments += ' -p1-65535'
    if options['dry_run']:
        print_line('nmap {0} {1}'.format(arguments, host))
        return ALLPORTS
    print_line('[+] Starting nmap')
    try:
        temp_file = next(tempfile._get_candidate_names())  # pylint: disable=protected-access
        arguments = '{0} -oN {1}'.format(arguments, temp_file)
        scanner = nmap.PortScanner()
        scanner.scan(hosts=host, arguments=arguments)
        for ip in [x for x in scanner.all_hosts() if scanner[x] and
                   scanner[x].state() == 'up']:
            open_ports = [port for port in scanner[ip].all_tcp() if
                          scanner[ip]['tcp'][port]['state'] == 'open']
        if options['no_portscan'] or len(open_ports):
            append_file(options, temp_file)
            if len(open_ports):
                print_line('    Found open ports {0}'.format(open_ports))
        else:
            print_status('Did not detect any open ports', options)
    except nmap.PortScannerError as exception:
        print_error('Issue with nmap ({0})'.format(exception))
        open_ports = [UNKNOWN]
    finally:
        os.remove(temp_file)
    return open_ports


def do_testssl(host, port, options):
    """
    Checks SSL/TLS configuration and vulnerabilities.
    """
    timeout = 120  # hardcoded for now
    command = ['testssl.sh', '--quiet', '--warnings', 'off', '--color', '0',
               '-p', '-f', '-U', '-S']
    if options['timeout']:
        command = ['timeout', str(timeout)] + command
    if port == 25:
        command += ['--starttls', 'smtp']
    print_status('Starting testssl.sh on {0}:{1}'.format(host, options),
                 options)
    _result, stdout, stderr = execute_command(command +
                                              ['{0}:{1}'.format(host, port)],
                                              options)  # pylint: disable=unused-variable
    append_logs(options, stdout, stderr)


def do_wpscan(url, options):
    """
    Runs WPscan.
    """
    if options['wpscan']:
        print_status('Starting WPscan on ' + url, options)
        command = ['wpscan', '--batch', '--no-color', '--url', url]
        _result, stdout, stderr = execute_command(command, options)  # pylint: disable=unused-variable
        append_logs(options, stdout, stderr)


def prepare_queue(options):
    """
    Prepares a queue file which holds all hosts to scan.
    """
    expanded = False
    if not options['inputfile']:
        expanded = next(tempfile._get_candidate_names())  # pylint: disable=protected-access
        with open(expanded, 'a') as inputfile:
            inputfile.write(options['target'])
        options['inputfile'] = expanded
    with open(options['inputfile'], 'r') as inputfile:
        hosts = inputfile.read().splitlines()
        targets = []
        for host in hosts:
            if ('/' in host) or ('-' in host):
                if not options['nmap']:
                    print_error('nmap is necessary for IP ranges', True)
                arguments = '-nsL'
                scanner = nmap.PortScanner()
                scanner.scan(hosts='{0}'.format(host), arguments=arguments)
                targets += sorted(scanner.all_hosts(),
                                  key=lambda x: tuple(map(int, x.split('.'))))
            else:
                targets.append(host)
        with open(options['queuefile'], 'a') as queuefile:
            for target in targets:
                queuefile.write(target + '\n')
    if expanded:
        os.remove(expanded)


def remove_from_queue(host, options):
    """
    Removes a host from the queue file.
    """
    with open(options['queuefile'], 'r+') as queuefile:
        hosts = queuefile.read().splitlines()
        queuefile.seek(0)
        for i in hosts:
            if i != host:
                queuefile.write(i + '\n')
        queuefile.truncate()
    if not os.stat(options['queuefile']).st_size:
        os.remove(options['queuefile'])


def port_open(port, open_ports):
    """
    Checks whether a port has been flagged as open
    Returns True if the port was open, or hasn't been scanned.

    Arguments:
    - `port`: the port to look up
    - `open_ports`: a list of open ports, or -1 if it hasn't been scanned.
    """
    return (UNKNOWN in open_ports) or (port in open_ports)


def use_tool(tool, host, port, options):
    """
    Wrapper to see if tool is available, and to start correct tool.
    """
    if not options[tool]:
        return
    print_status('starting {0} scan on {1}:{2}'.
                 format(tool, host, port), options)
    if tool == 'nikto':
        do_nikto(host, port, options)
    if tool == 'curl':
        do_curl(host, port, options)
    if tool == 'testssl.sh':
        do_testssl(host, port, options)


def loop_hosts(options, queue):
    """
    Main loop, iterates all hosts in queue.
    """
    for counter, host in enumerate(queue, 1):
        status = '[+] {0} Working on {1} ({2} of {3})'.format(timestamp(),
                                                              host, counter,
                                                              len(queue))
        if not options['dry_run']:
            print_line(status)
        append_logs(options, status + '\n')
        open_ports = do_portscan(host, options)
        for port in open_ports:
            if port in [80, 443, 8080]:
                for tool in ['curl', 'nikto']:
                    use_tool(tool, host, port, options)
                analyze_url(host, port, options)
            if port in [25, 443, 465, 993, 995]:
                for tool in ['testssl.sh']:
                    use_tool(tool, host, port, options)
                download_cert(host, port, options)
        status = '[-] {0} Finished {1} ({2} of {3})'.format(timestamp(),
                                                            host, counter,
                                                            len(queue))
        if not options['dry_run']:
            print_line(status)
        remove_from_queue(host, options)


def read_queue(filename):
    """
    Returns a list of targets.
    """
    queue = []
    try:
        with open(filename, 'r') as queuefile:
            queue = queuefile.read().splitlines()
    except IOError:
        print_line('[-] could not read {0}'.format(filename), True)
    return queue


def parse_arguments(banner):
    """
    Parses command line arguments.
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(banner + '''\
 - scans one or more hosts for security misconfigurations

Please note that this is NOT a stealthy scan tool: By default, a TCP and UDP
portscan will be launched, using some of nmap's interrogation scripts.

Copyright (C) 2015-2016  Peter Mosmans [Go Forward]
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.'''))
    parser.add_argument('target', nargs='?', type=str,
                        help="""[TARGET] can be a single (IP) address, an IP
                        range, eg. 127.0.0.1-255, or multiple comma-separated
                        addressess""")
    parser.add_argument('--dry-run', action='store_true',
                        help='only show commands, don\'t actually do anything')
    parser.add_argument('-i', '--inputfile', action='store', type=str,
                        help='a file containing targets, one per line')
    parser.add_argument('-o', '--output-file', action='store', type=str,
                        default='analyze_hosts.output',
                        help="""output file containing all scanresults
                        (default analyze_hosts.output""")
    parser.add_argument('--nikto', action='store_true',
                        help='run a nikto scan')
    parser.add_argument('-n', '--no-portscan', action='store_true',
                        help='do NOT run a nmap portscan')
    parser.add_argument('-p', '--port', action='store',
                        help='specific port(s) to scan')
    parser.add_argument('--compact', action='store_true',
                        help='log as little as possible')
    parser.add_argument('--queuefile', action='store',
                        default='analyze_hosts.queue', help='the queuefile')
    parser.add_argument('--resume', action='store_true',
                        help='resume working on the queue')
    parser.add_argument('--ssl', action='store_true',
                        help='run a ssl scan')
    parser.add_argument('--sslcert', action='store_true',
                        help='download SSL certificate')
    parser.add_argument('--udp', action='store_true',
                        help='check for open UDP ports as well')
    parser.add_argument('--framework', action='store_true',
                        help='analyze the website and run webscans')
    parser.add_argument('--allports', action='store_true',
                        help='run a full-blown nmap scan on all ports')
    parser.add_argument('-t', '--trace', action='store_true',
                        help='check webserver for HTTP TRACE method')
    parser.add_argument('-w', '--whois', action='store_true',
                        help='perform a whois lookup')
    parser.add_argument('--header', action='store', default='analyze_hosts',
                        help='custom header to use for scantools')
    parser.add_argument('--maxtime', action='store', default='600', type=int,
                        help='timeout for scans in seconds (default 600)')
    parser.add_argument('--timeout', action='store', default='10', type=int,
                        help='timeout for requests in seconds (default 10)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Be more verbose')
    args = parser.parse_args()
    if not (args.inputfile or args.target or args.resume):
        parser.error('Specify either a target or input file')
    options = vars(parser.parse_args())
    options['testssl.sh'] = args.ssl
    options['curl'] = args.trace
    options['wpscan'] = args.framework
    options['droopescan'] = args.framework
    return options


def main():
    """
    Main program loop.
    """
    banner = 'analyze_hosts.py version {0}'.format(VERSION)
    options = parse_arguments(banner)
    print_line(banner)
    preflight_checks(options)
    if not options['resume']:
        prepare_queue(options)
    queue = read_queue(options['queuefile'])
    loop_hosts(options, queue)
    if not options['dry_run']:
        print_line('{0} Output saved to {1}'.format(timestamp(),
                                                    options['output_file']))


if __name__ == "__main__":
    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, exit_gracefully)
    main()
