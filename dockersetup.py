#!/usr/bin/env python3

# Layer index Docker setup script
#
# Copyright (C) 2018 Intel Corporation
# Author: Amber Elliot <amber.n.elliot@intel.com>
#
# Licensed under the MIT license, see COPYING.MIT for details

# This script will make a cluster of 5 containers:
#
#  - layersapp: the application
#  - layersdb: the database
#  - layersweb: NGINX web server (as a proxy and for serving static content)
#  - layerscelery: Celery (for running background jobs)
#  - layersrabbit: RabbitMQ (required by Celery)
#
# It will build and run these containers and set up the database.

import sys

min_version = (3, 4, 3)
if sys.version_info < min_version:
    sys.stderr.write('Sorry, python version %d.%d.%d or later is required\n' % min_version)
    sys.exit(1)

import os
import argparse
import re
import subprocess
import time
import random
import shutil
import tempfile
from shlex import quote

def get_args():
    parser = argparse.ArgumentParser(description='Script sets up the Clear Linux* Dissector tool with Docker Containers.')

    parser.add_argument('-u', '--update', action="store_true", default=False, help='Update existing installation instead of installing')
    parser.add_argument('-r', '--reinstall', action="store_true", default=False, help='Reinstall over existing installation (wipes database!)')
    parser.add_argument('-o', '--hostname', type=str, help='Hostname of your machine. Defaults to localhost if not set.', required=False, default = "localhost")
    parser.add_argument('-p', '--http-proxy', type=str, help='http proxy in the format http://<myproxy:port>', required=False)
    parser.add_argument('-s', '--https-proxy', type=str, help='https proxy in the format http://<myproxy:port>', required=False)
    parser.add_argument('-d', '--databasefile', type=str, help='Location of your database file to import. Must be a .sql file.', required=False)
    parser.add_argument('-e', '--email-host', type=str, help='Email host for sending messages (optionally with :port if not 25)', required=False)
    parser.add_argument('-m', '--portmapping', type=str, help='Port mapping in the format HOST:CONTAINER. Default is %(default)s', required=False, default='8080:80,8081:443')
    parser.add_argument('--project-name', type=str, help='docker-compose project name to use')
    parser.add_argument('--no-https', action="store_true", default=False, help='Disable HTTPS (HTTP only) for web server')
    parser.add_argument('--cert', type=str, help='Existing SSL certificate to use for HTTPS web serving', required=False)
    parser.add_argument('--cert-key', type=str, help='Existing SSL certificate key to use for HTTPS web serving', required=False)
    parser.add_argument('--letsencrypt', action="store_true", default=False, help='Use Let\'s Encrypt for HTTPS')
    parser.add_argument('--no-migrate', action="store_true", default=False, help='Skip running database migrations')

    args = parser.parse_args()

    if args.update:
        if args.http_proxy or args.https_proxy or args.databasefile or args.no_https or args.cert or args.cert_key or args.letsencrypt:
            raise argparse.ArgumentTypeError("The -u/--update option will not update configuration or database content, and thus none of the other configuration options can be used in conjunction with it")
        if args.reinstall:
            raise argparse.ArgumentTypeError("The -u/--update and -r/--reinstall options are mutually exclusive")

    port = proxymod = ""
    try:
        if args.http_proxy:
            split = args.http_proxy.split(":")
            port = split[2]
            proxymod = split[1].replace("/", "")
    except IndexError:
        raise argparse.ArgumentTypeError("http_proxy must be in format http://<myproxy:port>")

    if args.http_proxy and not args.https_proxy:
        print('WARNING: http proxy specified without https proxy, this is likely to be incorrect')

    for entry in args.portmapping.split(','):
        if len(entry.split(":")) != 2:
            raise argparse.ArgumentTypeError("Port mapping must in the format HOST:CONTAINER. Ex: 8080:80. Multiple mappings should be separated by commas.")

    if args.no_https:
        if args.cert or args.cert_key or args.letsencrypt:
            raise argparse.ArgumentTypeError("--no-https and --cert/--cert-key/--letsencrypt options are mutually exclusive")
    if args.letsencrypt:
        if args.cert or args.cert_key:
            raise argparse.ArgumentTypeError("--letsencrypt and --cert/--cert-key options are mutually exclusive")
    if args.cert and not os.path.exists(args.cert):
        raise argparse.ArgumentTypeError("Specified certificate file %s does not exist" % args.cert)
    if args.cert_key and not os.path.exists(args.cert_key):
        raise argparse.ArgumentTypeError("Specified certificate key file %s does not exist" % args.cert_key)
    if args.cert_key and not args.cert:
        raise argparse.ArgumentTypeError("Certificate key file specified but not certificate")
    cert_key = args.cert_key
    if args.cert and not cert_key:
        cert_key = os.path.splitext(args.cert)[0] + '.key'
        if not os.path.exists(cert_key):
            raise argparse.ArgumentTypeError("Could not find certificate key, please use --cert-key to specify it")

    email_host = None
    email_port = None
    if args.email_host:
        email_host_split = args.email_host.split(':')
        email_host = email_host_split[0]
        if len(email_host_split) > 1:
            email_port = email_host_split[1]

    return args.update, args.reinstall, args.hostname, args.http_proxy, args.https_proxy, args.databasefile, port, proxymod, args.portmapping, args.no_https, args.cert, cert_key, args.letsencrypt, email_host, email_port, args.no_migrate, args.project_name

# Edit http_proxy and https_proxy in Dockerfile
def edit_dockerfile(http_proxy, https_proxy):
    filedata= readfile("Dockerfile")
    newlines = []
    lines = filedata.splitlines()
    for line in lines:
        if "ENV http_proxy" in line and http_proxy:
            newlines.append("ENV http_proxy " + http_proxy + "\n")
        elif "ENV https_proxy" in line and https_proxy:
            newlines.append("ENV https_proxy " + https_proxy + "\n")
        else:
            newlines.append(line + "\n")

    writefile("Dockerfile", ''.join(newlines))


# If using a proxy, add proxy values to git-proxy and uncomment proxy script in .gitconfig
def edit_gitproxy(proxymod, port):
    filedata= readfile("docker/git-proxy")
    newlines = []
    lines = filedata.splitlines()
    for line in lines:
        if "PROXY=" in line:
            newlines.append("PROXY=" + proxymod + "\n")
        elif "PORT=" in line:
            newlines.append("PORT=" + port + "\n")
        else:
            newlines.append(line + "\n")
    writefile("docker/git-proxy", ''.join(newlines))
    filedata = readfile("docker/.gitconfig")
    newdata = filedata.replace("#gitproxy", "gitproxy")
    writefile("docker/.gitconfig", newdata)

def yaml_uncomment(line):
    out = ''
    for i, ch in enumerate(line):
        if ch == ' ':
            out += ch
        elif ch != '#':
            out += line[i:]
            break
    return out

def yaml_comment(line):
    out = ''
    commented = False
    for i, ch in enumerate(line):
        if ch == '#':
            commented = True
            out += line[i:]
            break
        elif ch != ' ':
            if not commented:
                out += '#'
            out += line[i:]
            break
        else:
            out += ch
    return out


# Add hostname, secret key, db info, and email host in docker-compose.yml
def edit_dockercompose(hostname, dbpassword, dbapassword, secretkey, rmqpassword, portmapping, letsencrypt, email_host, email_port):
    filedata= readfile("docker-compose.yml")
    in_layersweb = False
    in_layersweb_ports = False
    in_layersweb_ports_format = None
    in_layerscertbot_format = None
    newlines = []
    lines = filedata.splitlines()
    for line in lines:
        if in_layersweb_ports:
            format = line[0:line.find("-")].replace("#", "")
            if in_layersweb_ports_format:
                if format != in_layersweb_ports_format:
                    in_layersweb_ports = False
                    in_layersweb = False
                else:
                    continue
            else:
                in_layersweb_ports_format = format
                for portmap in portmapping.split(','):
                    newlines.append(format + '- "' + portmap + '"' + "\n")
                continue
        if in_layerscertbot_format:
            ucline = yaml_uncomment(line)
            format = re.match(r'^( *)', ucline).group(0)
            if len(format) <= len(in_layerscertbot_format):
                in_layerscertbot_format = False
            elif letsencrypt:
                newlines.append(ucline + '\n')
                continue
            else:
                newlines.append(yaml_comment(line) + '\n')
                continue
        if "layerscertbot:" in line:
            ucline = yaml_uncomment(line)
            in_layerscertbot_format = re.match(r'^( *)', ucline).group(0)
            if letsencrypt:
                newlines.append(ucline + '\n')
            else:
                newlines.append(yaml_comment(line) + '\n')
        elif "layersweb:" in line:
            in_layersweb = True
            newlines.append(line + "\n")
        elif "hostname:" in line:
            format = line[0:line.find("hostname")].replace("#", "")
            newlines.append(format +"hostname: " + hostname + "\n")
        elif '- "SECRET_KEY' in line:
            format = line[0:line.find('- "SECRET_KEY')].replace("#", "")
            newlines.append(format + '- "SECRET_KEY=' + secretkey + '"\n')
        elif '- "DATABASE_USER' in line:
            format = line[0:line.find('- "DATABASE_USER')].replace("#", "")
            newlines.append(format + '- "DATABASE_USER=layers"\n')
        elif '- "DATABASE_PASSWORD' in line:
            format = line[0:line.find('- "DATABASE_PASSWORD')].replace("#", "")
            newlines.append(format + '- "DATABASE_PASSWORD=' + dbpassword + '"\n')
        elif '- "MYSQL_ROOT_PASSWORD' in line:
            format = line[0:line.find('- "MYSQL_ROOT_PASSWORD')].replace("#", "")
            newlines.append(format + '- "MYSQL_ROOT_PASSWORD=' + dbapassword + '"\n')
        elif '- "RABBITMQ_DEFAULT_USER' in line:
            format = line[0:line.find('- "RABBITMQ_DEFAULT_USER')].replace("#", "")
            newlines.append(format + '- "RABBITMQ_DEFAULT_USER=layermq"\n')
        elif '- "RABBITMQ_DEFAULT_PASS' in line:
            format = line[0:line.find('- "RABBITMQ_DEFAULT_PASS')].replace("#", "")
            newlines.append(format + '- "RABBITMQ_DEFAULT_PASS=' + rmqpassword + '"\n')
        elif '- "EMAIL_HOST' in line:
            format = line[0:line.find('- "EMAIL_HOST')].replace("#", "")
            if email_host:
                newlines.append(format + '- "EMAIL_HOST=' + email_host + '"\n')
            else:
                newlines.append(format + '#- "EMAIL_HOST=<set this here>"\n')
        elif '- "EMAIL_PORT' in line:
            format = line[0:line.find('- "EMAIL_PORT')].replace("#", "")
            if email_port:
                newlines.append(format + '- "EMAIL_PORT=' + email_port + '"\n')
            else:
                newlines.append(format + '#- "EMAIL_PORT=<set this here if not the default>"\n')
        elif "ports:" in line:
            if in_layersweb:
                in_layersweb_ports = True
            newlines.append(line + "\n")
        elif letsencrypt and "./docker/certs:/" in line:
            newlines.append(line.split(':')[0] + ':/etc/letsencrypt\n')
        else:
            newlines.append(line + "\n")
    writefile("docker-compose.yml", ''.join(newlines))


def read_nginx_ssl_conf(certdir):
    hostname = None
    https_port = None
    certdir = None
    certfile = None
    keyfile = None
    with open('docker/nginx-ssl-edited.conf', 'r') as f:
        for line in f:
            if 'ssl_certificate ' in line:
                certdir, certfile = os.path.split(line.split('ssl_certificate', 1)[1].strip().rstrip(';'))
            elif 'ssl_certificate_key ' in line:
                keyfile = os.path.basename(line.split('ssl_certificate_key', 1)[1].strip().rstrip(';'))
            elif 'server_name ' in line:
                sname = line.split('server_name', 1)[1].strip().rstrip(';')
                if sname != '_':
                    hostname = sname
            elif 'return 301 https://' in line:
                res = re.search(':([0-9]+)', line)
                if res:
                    https_port = res.groups()[0]
    ret = (hostname, https_port, certdir, certfile, keyfile)
    if None in ret:
        sys.stderr.write('Failed to read SSL configuration from nginx-ssl-edited.conf')
        sys.exit(1)
    return ret

def edit_nginx_ssl_conf(hostname, https_port, certdir, certfile, keyfile):
    filedata = readfile('docker/nginx-ssl.conf')
    newlines = []
    lines = filedata.splitlines()
    for line in lines:
        if 'ssl_certificate ' in line:
            format = line[0:line.find('ssl_certificate')]
            newlines.append(format + 'ssl_certificate ' + os.path.join(certdir, certfile) + ';\n')
        elif 'ssl_certificate_key ' in line:
            format = line[0:line.find('ssl_certificate_key')]
            newlines.append(format + 'ssl_certificate_key ' + os.path.join(certdir, keyfile) + ';\n')
            # Add a line for the dhparam file
            newlines.append(format + 'ssl_dhparam ' + os.path.join(certdir, 'dhparam.pem') + ';\n')
        elif 'https://layers.openembedded.org' in line:
            line = line.replace('https://layers.openembedded.org', 'https://%s:%s' % (hostname, https_port))
            newlines.append(line + "\n")
        else:
            line = line.replace('layers.openembedded.org', hostname)
            newlines.append(line + "\n")

    # Write to a different file so we can still replace the hostname next time
    writefile("docker/nginx-ssl-edited.conf", ''.join(newlines))


def edit_settings_py(emailaddr, no_https):
    filedata = readfile('docker/settings.py')
    newlines = []
    lines = filedata.splitlines()
    in_admins = False
    for line in lines:
        if in_admins:
            if line.strip() == ')':
                in_admins = False
            continue
        elif 'SESSION_COOKIE_SECURE' in line:
            line = line.lstrip('#')
            if no_https:
                line = '#' + line
        elif 'CSRF_COOKIE_SECURE' in line:
            line = line.lstrip('#')
            if no_https:
                line = '#' + line
        elif line.lstrip().startswith('ADMINS = ('):
            if line.count('(') > line.count(')'):
                in_admins = True
            newlines.append("ADMINS = (\n")
            if emailaddr:
                newlines.append("  ('Admin', '%s'),\n" % emailaddr)
            newlines.append(")\n")
            continue
        newlines.append(line + "\n")
    writefile("docker/settings.py", ''.join(newlines))


def read_dockerfile_web():
    no_https = True
    with open('Dockerfile.web', 'r') as f:
        for line in f:
            if line.startswith('COPY ') and line.rstrip().endswith('/etc/nginx/nginx.conf'):
                if 'nginx-ssl' in line:
                    no_https = False
                break
    return no_https


def edit_dockerfile_web(hostname, no_https):
    filedata = readfile('Dockerfile.web')
    newlines = []
    lines = filedata.splitlines()
    for line in lines:
        if line.startswith('COPY ') and line.endswith('/etc/nginx/nginx.conf'):
            if no_https:
                srcfile = 'docker/nginx.conf'
            else:
                srcfile = 'docker/nginx-ssl-edited.conf'
            line = 'COPY %s /etc/nginx/nginx.conf' % srcfile
        newlines.append(line + "\n")
    writefile("Dockerfile.web", ''.join(newlines))


def setup_https(hostname, http_port, https_port, letsencrypt, cert, cert_key, emailaddr):
    local_cert_dir = os.path.abspath('docker/certs')
    container_cert_dir = '/opt/cert'
    if letsencrypt:
        # Create dummy cert
        container_cert_dir = '/etc/letsencrypt'
        letsencrypt_cert_subdir = 'live/' + hostname
        local_letsencrypt_cert_dir = os.path.join(local_cert_dir, letsencrypt_cert_subdir)
        if not os.path.isdir(local_letsencrypt_cert_dir):
            os.makedirs(local_letsencrypt_cert_dir)
        keyfile = os.path.join(letsencrypt_cert_subdir, 'privkey.pem')
        certfile = os.path.join(letsencrypt_cert_subdir, 'fullchain.pem')
        return_code = subprocess.call(['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:1024', '-days', '1', '-keyout', os.path.join(local_cert_dir, keyfile), '-out', os.path.join(local_cert_dir, certfile), '-subj', '/CN=localhost'], shell=False)
        if return_code != 0:
            print("Dummy certificate generation failed")
            sys.exit(1)
    elif cert:
        if os.path.abspath(os.path.dirname(cert)) != local_cert_dir:
            shutil.copy(cert, local_cert_dir)
        certfile = os.path.basename(cert)
        if os.path.abspath(os.path.dirname(cert_key)) != local_cert_dir:
            shutil.copy(cert_key, local_cert_dir)
        keyfile = os.path.basename(cert_key)
    else:
        print('')
        print('Generating self-signed SSL certificate. Please specify your hostname (%s) when prompted for the Common Name.' % hostname)
        certfile = 'setup-selfsigned.crt'
        keyfile = 'setup-selfsigned.key'
        return_code = subprocess.call(['openssl', 'req', '-x509', '-nodes', '-days', '365', '-newkey', 'rsa:2048', '-keyout', os.path.join(local_cert_dir, keyfile), '-out', os.path.join(local_cert_dir, certfile)], shell=False)
        if return_code != 0:
            print("Self-signed certificate generation failed")
            sys.exit(1)
    return_code = subprocess.call(['openssl', 'dhparam', '-out', os.path.join(local_cert_dir, 'dhparam.pem'), '2048'], shell=False)
    if return_code != 0:
        print("DH group generation failed")
        sys.exit(1)

    edit_nginx_ssl_conf(hostname, https_port, container_cert_dir, certfile, keyfile)

    if letsencrypt:
        return_code = subprocess.call(['docker-compose', 'up', '-d', '--build', 'layersweb'], shell=False)
        if return_code != 0:
            print("docker-compose up layersweb failed")
            sys.exit(1)
        tempdir = tempfile.mkdtemp()
        try:
            # Wait for web server to start
            while True:
                time.sleep(2)
                return_code = subprocess.call(['wget', '-q', '--no-check-certificate', "http://{}:{}/".format(hostname, http_port)], shell=False, cwd=tempdir)
                if return_code == 0 or return_code > 4:
                    break
                else:
                    print("Web server may not be ready; will try again.")

            # Delete temp cert now that the server is up
            shutil.rmtree(os.path.join(local_cert_dir, 'live'))

            # Create a test file and fetch it to ensure web server is working (for http)
            return_code = subprocess.call("docker-compose exec -T layersweb /bin/sh -c 'mkdir -p /var/www/certbot/.well-known/acme-challenge/ ; echo something > /var/www/certbot/.well-known/acme-challenge/test.txt'", shell=True)
            if return_code != 0:
                print("Creating test file failed")
                sys.exit(1)
            return_code = subprocess.call(['wget', '-nv', "http://{}:{}/.well-known/acme-challenge/test.txt".format(hostname, http_port)], shell=False, cwd=tempdir)
            if return_code != 0:
                print("Reading test file from web server failed")
                sys.exit(1)
            return_code = subprocess.call(['docker-compose', 'exec', '-T', 'layersweb', '/bin/sh', '-c', 'rm -rf /var/www/certbot/.well-known'], shell=False)
            if return_code != 0:
                print("Removing test file failed")
                sys.exit(1)
        finally:
            shutil.rmtree(tempdir)

        # Now run certbot to register SSL certificate
        staging_arg = '--staging'
        if emailaddr:
            email_arg = '--email %s' % quote(emailaddr)
        else:
            email_arg = '--register-unsafely-without-email'
        return_code = subprocess.call('docker-compose run --rm --entrypoint "\
  certbot certonly --webroot -w /var/www/certbot \
    %s \
    %s \
    -d %s \
    --rsa-key-size 4096 \
    --agree-tos \
    --force-renewal" layerscertbot' % (staging_arg, email_arg, quote(hostname)), shell=True)
        if return_code != 0:
            print("Running certbot failed")
            sys.exit(1)

        # Stop web server (so it can effectively be restarted with the new certificate)
        return_code = subprocess.call(['docker-compose', 'stop', 'layersweb'], shell=False)
        if return_code != 0:
            print("docker-compose stop failed")
            sys.exit(1)


def edit_options_file(project_name):
    with open('.dockersetup-options', 'w') as f:
        f.write('project_name=%s\n' % project_name)


def generatepasswords(passwordlength):
    return ''.join([random.SystemRandom().choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@%^&*-_+') for i in range(passwordlength)])

def readfile(filename):
    with open(filename, 'r') as f:
        return f.read()

def writefile(filename, data):
    with open(filename, 'w') as f:
        f.write(data)


## Get user arguments and modify config files
updatemode, reinstmode, hostname, http_proxy, https_proxy, dbfile, port, proxymod, portmapping, no_https, cert, cert_key, letsencrypt, email_host, email_port, no_migrate, project_name = get_args()

if updatemode:
    with open('docker-compose.yml', 'r') as f:
        for line in f:
            if 'MYSQL_ROOT_PASSWORD=' in line:
                dbapassword = line.split('=')[1].rstrip().rstrip('"')
                break
    # Use last project name
    try:
        with open('.dockersetup-options', 'r') as f:
            for line in f:
                if line.startswith('project_name='):
                    project_name = line.split('=', 1)[1].rstrip()
    except FileNotFoundError:
        pass
else:
    # Generate secret key and database password
    secretkey = generatepasswords(50)
    dbapassword = generatepasswords(10)
    dbpassword = generatepasswords(10)
    rmqpassword = generatepasswords(10)

if project_name:
    os.environ['COMPOSE_PROJECT_NAME'] = project_name
else:
    # Get the project name from the environment (so we can save it for a future upgrade)
    project_name = os.environ.get('COMPOSE_PROJECT_NAME', '')

https_port = None
http_port = None
if not updatemode:
    for portmap in portmapping.split(','):
        outport, inport = portmap.split(':', 1)
        if inport == '443':
            https_port = outport
        elif inport == '80':
            http_port = outport
    if (not https_port) and (not no_https):
        print("No HTTPS port mapping (to port 443 inside the container) was specified and --no-https was not specified")
        sys.exit(1)
    if not http_port:
        print("Port mapping must include a mapping to port 80 inside the container")
        sys.exit(1)

## Check if it's installed
installed = False
return_code = subprocess.call("docker ps -a | grep -q layersapp", shell=True)
if return_code == 0:
    installed = True

if updatemode:
    if not installed:
        print("Application container not found - update mode can only be used on an existing installation")
        sys.exit(1)
    if dbapassword == 'testingpw':
        print("Update mode can only be used when previous configuration is still present in docker-compose.yml and other files")
        sys.exit(1)
elif installed and not reinstmode:
    print('Application already installed. Please use -u/--update to update or -r/--reinstall to reinstall')
    sys.exit(1)

print("""
Clear Linux* Dissector Docker setup script
------------------------------------------

This script will set up a cluster of Docker containers needed to run the
Clear Linux* Dissector application.

Configuration is controlled by command-line arguments. If you need to check
which options you need to specify, press Ctrl+C now and then run the script
again with the --help argument.

Note that this script does have interactive prompts, so be prepared to
provide information as needed.
""")

if reinstmode:
    print("""  WARNING: continuing will wipe out any existing data in the database and set
  up the application from scratch! Press Ctrl+C now if this is not what you
  want.
""")

try:
    if updatemode:
        promptstr = 'Press Enter to begin update (or Ctrl+C to exit)...'
    else:
        promptstr = 'Press Enter to begin setup (or Ctrl+C to exit)...'
    input(promptstr)
except KeyboardInterrupt:
    print('')
    sys.exit(2)

if not updatemode:
    # Get email address
    print('')
    if letsencrypt:
        print('You will now be asked for an email address. This will be used for the superuser account, to send error reports to and for Let\'s Encrypt.')
    else:
        print('You will now be asked for an email address. This will be used for the superuser account and to send error reports to.')
    emailaddr = None
    while True:
        emailaddr = input('Enter your email address: ')
        if '@' in emailaddr:
            break
        else:
            print('Entered email address is not valid')

if reinstmode:
    return_code = subprocess.call(['docker-compose', 'down', '-v'], shell=False)

if updatemode:
    no_https = read_dockerfile_web()
    if not no_https:
        container_cert_dir = '/opt/cert'
        hostname, https_port, certdir, certfile, keyfile = read_nginx_ssl_conf(container_cert_dir)
        edit_nginx_ssl_conf(hostname, https_port, certdir, certfile, keyfile)
else:
    if http_proxy:
        edit_gitproxy(proxymod, port)
    if http_proxy or https_proxy:
        edit_dockerfile(http_proxy, https_proxy)

    edit_dockercompose(hostname, dbpassword, dbapassword, secretkey, rmqpassword, portmapping, letsencrypt, email_host, email_port)

    edit_dockerfile_web(hostname, no_https)

    edit_settings_py(emailaddr, no_https)

    edit_options_file(project_name)

    if not no_https:
        setup_https(hostname, http_port, https_port, letsencrypt, cert, cert_key, emailaddr)

## Start up containers
return_code = subprocess.call(['docker-compose', 'up', '-d', '--build'], shell=False)
if return_code != 0:
    print("docker-compose up failed")
    sys.exit(1)

# Get real project name (if only there were a reasonable way to do this... ugh)
real_project_name = ''
output = subprocess.check_output(['docker-compose', 'ps', '-q'], shell=False)
if output:
    output = output.decode('utf-8')
    for contid in output.splitlines():
        output = subprocess.check_output(['docker', 'inspect', '-f', '{{ .Mounts }}', contid], shell=False)
        if output:
            output = output.decode('utf-8')
            for volume in re.findall('volume ([^ ]+)', output):
                if '_' in volume:
                    real_project_name = volume.rsplit('_', 1)[0]
                    break
            if real_project_name:
                break
if not real_project_name:
    print('Failed to detect docker-compose project name')
    sys.exit(1)

# Database might not be ready yet; have to wait then poll.
time.sleep(8)
while True:
    time.sleep(2)
    # Pass credentials through environment for slightly better security
    # (avoids password being visible through ps or /proc/<pid>/cmdline)
    env = os.environ.copy()
    env['MYSQL_PWD'] = dbapassword
    # Dummy command, we just want to establish that the db can be connected to
    return_code = subprocess.call("echo | docker-compose exec -T -e MYSQL_PWD layersdb mysql -uroot layersdb", shell=True, env=env)
    if return_code == 0:
        break
    else:
        print("Database server may not be ready; will try again.")

if not updatemode:
    # Import the user's supplied data
    if dbfile:
        return_code = subprocess.call("gunzip -t %s > /dev/null 2>&1" % quote(dbfile), shell=True)
        if return_code == 0:
            catcmd = 'zcat'
        else:
            catcmd = 'cat'
        env = os.environ.copy()
        env['MYSQL_PWD'] = dbapassword
        return_code = subprocess.call("%s %s | docker-compose exec -T -e MYSQL_PWD layersdb mysql -uroot layersdb" % (catcmd, quote(dbfile)), shell=True, env=env)
        if return_code != 0:
            print("Database import failed")
            sys.exit(1)

if not no_migrate:
    # Apply any pending layerindex migrations / initialize the database.
    env = os.environ.copy()
    env['DATABASE_USER'] = 'root'
    env['DATABASE_PASSWORD'] = dbapassword
    return_code = subprocess.call(['docker-compose', 'run', '--rm', '-e', 'DATABASE_USER', '-e', 'DATABASE_PASSWORD', 'layersapp', '/opt/migrate.sh'], shell=False, env=env)
    if return_code != 0:
        print("Applying migrations failed")
        sys.exit(1)

if not updatemode:
    # Create normal database user for app to use
    with tempfile.NamedTemporaryFile('w', dir=os.getcwd(), delete=False) as tf:
        sqlscriptfile = tf.name
        tf.write("DROP USER IF EXISTS layers;")
        tf.write("CREATE USER layers IDENTIFIED BY '%s';\n" % dbpassword)
        tf.write("GRANT SELECT, UPDATE, INSERT, DELETE ON layersdb.* TO layers;\n")
        tf.write("FLUSH PRIVILEGES;\n")
    try:
        # Pass credentials through environment for slightly better security
        # (avoids password being visible through ps or /proc/<pid>/cmdline)
        env = os.environ.copy()
        env['MYSQL_PWD'] = dbapassword
        return_code = subprocess.call("docker-compose exec -T -e MYSQL_PWD layersdb mysql -uroot layersdb < " + quote(sqlscriptfile), shell=True, env=env)
        if return_code != 0:
            print("Creating database user failed")
            sys.exit(1)
    finally:
        os.remove(sqlscriptfile)

    ## Set the volume permissions using debian:stretch since we recently fetched it
    volumes = ['layersmeta', 'layersstatic', 'patchvolume', 'logvolume']
    with open('docker-compose.yml', 'r') as f:
        for line in f:
            if line.lstrip().startswith('- srcvolume:'):
                volumes.append('srcvolume')
                break
    for volume in volumes:
        volname = '%s_%s' % (real_project_name, volume)
        return_code = subprocess.call(['docker', 'run', '--rm', '-v', '%s:/opt/mount' % volname, 'debian:stretch', 'chown', '500', '/opt/mount'], shell=False)
        if return_code != 0:
            print("Setting volume permissions for volume %s failed" % volume)
            sys.exit(1)

## Generate static assets. Run this command again to regenerate at any time (when static assets in the code are updated)
return_code = subprocess.call("docker-compose run --rm -e STATIC_ROOT=/usr/share/nginx/html -v %s_layersstatic:/usr/share/nginx/html layersapp /opt/layerindex/manage.py collectstatic --noinput" % quote(real_project_name), shell = True)
if return_code != 0:
    print("Collecting static files failed")
    sys.exit(1)

if not updatemode:
    ## Set site name
    return_code = subprocess.call(['docker-compose', 'run', '--rm', 'layersapp', '/opt/layerindex/layerindex/tools/site_name.py', hostname, 'Clear Linux* Dissector'], shell=False)

    ## For a fresh database, create an admin account
    print("Creating database superuser. Input user name and password when prompted.")
    return_code = subprocess.call(['docker-compose', 'run', '--rm', 'layersapp', '/opt/layerindex/manage.py', 'createsuperuser', '--email', emailaddr], shell=False)
    if return_code != 0:
        print("Creating superuser failed")
        sys.exit(1)


if updatemode:
    print("Update complete")
else:
    if project_name:
        print("")
        print("NOTE: you may need to use -p %s (or set COMPOSE_PROJECT_NAME=\"%s\" ) if running docker-compose directly in future" % (project_name, project_name))
    print("")
    if https_port and not no_https:
        protocol = 'https'
        port = https_port
    else:
        protocol = 'http'
        port = http_port
    print("The application should now be accessible at %s://%s:%s" % (protocol, hostname, port))
    print("")
