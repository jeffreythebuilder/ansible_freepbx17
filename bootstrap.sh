#!/bin/bash
set -e

echo "Installing Ansible..."
apt-get update -y
apt-get install -y ansible git

echo "Cloning playbook..."
git clone https://github.com/jeffreythebuilder/ansible_freepbx17.git /root/playbook

echo "Running playbook..."
ansible-playbook /root/playbook/site.yml
