#!/bin/bash

echo "Waiting for network..."
until ping -c1 github.com &>/dev/null; do
    echo "Network not ready, retrying in 5 seconds..."
    sleep 5
done
echo "Network is up!"

echo "Installing dependencies..."
apt-get update -y
apt-get install -y ansible git

echo "Cloning playbook..."
rm -rf /root/playbook
git clone https://github.com/jeffreythebuilder/ansible_freepbx17.git /root/playbook

echo "Running playbook..."
mkdir -p /dev/shm
ansible-playbook -i localhost, -c local /root/playbook/site.yml

echo "Enabling SSH root login..."
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
systemctl restart sshd

echo "Rebooting..."
reboot
