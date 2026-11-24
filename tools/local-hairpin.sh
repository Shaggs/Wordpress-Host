#!/bin/bash
# Keep the server reaching its own sites by their public address locally, without the firewall.
# Needed before unticking "Force translated traffic to return to the gateway" on the Check Point
# server object (which is what hides every visitor's real address behind the firewall's own).
#
#   sudo bash local-hairpin.sh status    show what is in place and test it
#   sudo bash local-hairpin.sh install   add the rule now and on every boot (systemd unit)
#   sudo bash local-hairpin.sh remove    take it out again
#
# The rule: traffic from this server or its containers to PUBLIC_IP ports 80/443 is sent straight
# to this server (LAN_IP) instead of out to the firewall and back. Nothing else is touched.
set -u
# Set both before running, e.g.  sudo PUBLIC_IP=203.0.113.10 LAN_IP=192.0.2.10 bash local-hairpin.sh install
PUBLIC_IP=${PUBLIC_IP:?set PUBLIC_IP (the public address of the sites)}
LAN_IP=${LAN_IP:?set LAN_IP (the LAN address of this server)}
CHAIN=LOCAL-HAIRPIN
UNIT=/etc/systemd/system/local-hairpin.service
ME=/usr/local/sbin/local-hairpin

apply() {
  iptables -t nat -N $CHAIN 2>/dev/null || iptables -t nat -F $CHAIN
  iptables -t nat -A $CHAIN -d $PUBLIC_IP/32 -p tcp -m multiport --dports 80,443 -j DNAT --to-destination $LAN_IP
  iptables -t nat -C PREROUTING -j $CHAIN 2>/dev/null || iptables -t nat -I PREROUTING 1 -j $CHAIN   # containers
  iptables -t nat -C OUTPUT     -j $CHAIN 2>/dev/null || iptables -t nat -I OUTPUT 1 -j $CHAIN       # the server itself
}
unapply() {
  iptables -t nat -D PREROUTING -j $CHAIN 2>/dev/null
  iptables -t nat -D OUTPUT -j $CHAIN 2>/dev/null
  iptables -t nat -F $CHAIN 2>/dev/null; iptables -t nat -X $CHAIN 2>/dev/null
}
test_it() {
  # use a real site name: the proxy refuses HTTPS requests that do not name one of its sites
  local ok=0 code out d
  d=${TEST_DOMAIN:-$(grep -hoP 'server_name\s+\K[^ ;]+' /opt/wp-host/nginx-proxy-manager/data/nginx/proxy_host/*.conf 2>/dev/null | grep -v '^www\.' | head -1)}
  [ -n "$d" ] || { echo "  no site found to test with (set TEST_DOMAIN=...)"; return 1; }
  code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' --resolve "$d:443:$PUBLIC_IP" "https://$d/")
  echo "  this server  -> https://$d via $PUBLIC_IP : HTTP $code   (200/301/302 = working, 000 = not)"
  [ "$code" != "000" ] || ok=1
  out=$(docker run --rm --add-host "$d:$PUBLIC_IP" --entrypoint wget wordpress:cli -S -T 10 -O /dev/null "https://$d/" 2>&1 | grep -m1 'HTTP/')
  echo "  a container  -> https://$d via $PUBLIC_IP : ${out:-no answer}"
  [ -n "$out" ] || ok=1
  return $ok
}

case "${1:-status}" in
  install)
    ss -ltn | grep -qE "[:.]443[[:space:]]" || { echo "nothing listens on port 443 on this server - stop"; exit 1; }
    install -m 755 "$(readlink -f "$0")" $ME
    cat > $UNIT <<EOF
[Unit]
Description=Reach this server's own sites locally (public address $PUBLIC_IP -> $LAN_IP)
After=docker.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
Environment=PUBLIC_IP=$PUBLIC_IP LAN_IP=$LAN_IP
ExecStart=$ME apply
ExecStop=$ME unapply
[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload && systemctl enable --now local-hairpin.service
    echo "installed (applies now and on every boot)"; iptables -t nat -S $CHAIN; test_it ;;
  apply)   apply ;;
  unapply) unapply ;;
  remove)  systemctl disable --now local-hairpin.service 2>/dev/null; rm -f $UNIT; systemctl daemon-reload; unapply; echo "removed" ;;
  status)  iptables -t nat -S $CHAIN 2>/dev/null || echo "rule not in place"; systemctl is-enabled local-hairpin.service 2>/dev/null; test_it ;;
  *) echo "usage: $0 status|install|remove"; exit 2 ;;
esac
