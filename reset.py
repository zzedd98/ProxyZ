"""
Reset modem optimisé (API directe uniquement, sans fallback).

Usage:
    python reset.py <proxy_port>
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import base64

import httpx

MODEM_HOST = "192.168.8.1"
MODEM_BASE = f"http://{MODEM_HOST}"
PROFILE_API = f"{MODEM_BASE}/api/dialup/profiles"
SESTOK_API = f"{MODEM_BASE}/api/webserver/SesTokInfo"
PUBLICKEY_API = f"{MODEM_BASE}/api/webserver/publickey"
STATE_LOGIN_API = f"{MODEM_BASE}/api/user/state-login"
MONITOR_STATUS_API = f"{MODEM_BASE}/api/monitoring/status"
DIAL_API = f"{MODEM_BASE}/api/dialup/dial"
DATASWITCH_API = f"{MODEM_BASE}/api/dialup/mobile-dataswitch"


def _get_modem_ip(proxy_port: int) -> str | None:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    services = ["https://api.ipify.org", "https://ifconfig.me", "https://icanhazip.com"]
    for service in services:
        try:
            with httpx.Client(proxy=proxy_url, timeout=10.0) as client:
                response = client.get(service)
                if response.status_code != 200:
                    continue
                ip = response.text.strip()
                if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                    return ip
        except Exception:
            continue
    return None


def _xml_text(root: ET.Element, tag: str, default: str = "") -> str:
    node = root.find(tag)
    if node is None or node.text is None:
        return default
    return node.text.strip()


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Origin": MODEM_BASE,
        "Referer": f"{MODEM_BASE}/",
        "_ResponseSource": "Broswer",
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": token,
    }


def _latest_token_from_response(resp: httpx.Response, current_token: str) -> str:
    return (
        resp.headers.get("__RequestVerificationToken")
        or resp.headers.get("__RequestVerificationTokenone")
        or current_token
    )


def _get_session_and_token(client: httpx.Client) -> tuple[str, str]:
    r = client.get(SESTOK_API, timeout=15.0)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    ses = _xml_text(root, "SesInfo")
    tok = _xml_text(root, "TokInfo")
    if not tok:
        raise RuntimeError("TokInfo manquant")
    return ses, tok


def _get_publickey(client: httpx.Client) -> tuple[str, str]:
    r = client.get(PUBLICKEY_API, timeout=15.0)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    n_hex = _xml_text(root, "encpubkeyn")
    e_hex = _xml_text(root, "encpubkeye")
    if not n_hex or not e_hex:
        raise RuntimeError("Clé publique modem introuvable")
    return n_hex, e_hex


def _get_rsa_padding_type(client: httpx.Client) -> int:
    # 1 => OAEP (vu dans main.js), sinon PKCS#1 v1.5
    try:
        r = client.get(STATE_LOGIN_API, timeout=15.0)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        v = _xml_text(root, "rsapadingtype", "0")
        return int(v) if v.isdigit() else 0
    except Exception:
        return 0


def _get_profiles(client: httpx.Client) -> tuple[str, list[ET.Element]]:
    r = client.get(PROFILE_API, timeout=15.0)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    current_profile = _xml_text(root, "CurrentProfile", "1")
    profiles_parent = root.find("Profiles")
    if profiles_parent is None:
        raise RuntimeError("Profiles manquant")
    profiles = list(profiles_parent.findall("Profile"))
    if not profiles:
        raise RuntimeError("Aucun profil trouvé")
    return current_profile, profiles


def _build_set_profile_request(target_profile: ET.Element, nonce: str = "") -> str:
    req = ET.Element("request")
    ET.SubElement(req, "Delete").text = "0"
    ET.SubElement(req, "Modify").text = "1"
    ET.SubElement(req, "SetDefault").text = "1"
    ET.SubElement(req, "CurrentProfile").text = _xml_text(target_profile, "Index", "1")

    profile_node = ET.SubElement(req, "Profile")
    for tag in (
        "Index",
        "IsValid",
        "Name",
        "ApnIsStatic",
        "ApnName",
        "DialupNum",
        "Username",
        "Password",
        "AuthMode",
        "IpIsStatic",
        "IpAddress",
        "Ipv6Address",
        "DnsIsStatic",
        "PrimaryDns",
        "PrimaryIpv6Dns",
        "SecondaryDns",
        "SecondaryIpv6Dns",
        "ReadOnly",
        "iptype",
    ):
        ET.SubElement(profile_node, tag).text = _xml_text(target_profile, tag, "")
    if nonce:
        ET.SubElement(req, "nonce").text = nonce
    body = ET.tostring(req, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>{body}'


def _build_set_profile_request_minimal(target_profile: ET.Element, nonce: str = "") -> str:
    req = ET.Element("request")
    ET.SubElement(req, "CurrentProfile").text = _xml_text(target_profile, "Index", "1")
    if nonce:
        ET.SubElement(req, "nonce").text = nonce
    body = ET.tostring(req, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>{body}'


def _pick_next_profile(current_profile: str, profiles: list[ET.Element]) -> ET.Element:
    current = current_profile.strip()
    for p in profiles:
        if _xml_text(p, "Index", "") != current:
            return p
    return profiles[0]


def _pkcs1_v15_pad(block: bytes, k: int) -> bytes:
    ps_len = k - len(block) - 3
    if ps_len < 8:
        raise ValueError("Bloc trop long pour RSA PKCS#1 v1.5")
    ps = bytearray()
    while len(ps) < ps_len:
        b = os.urandom(1)
        if b != b"\x00":
            ps.extend(b)
    return b"\x00\x02" + bytes(ps) + b"\x00" + block


def _rsa_encrypt_hex_chunks(data: bytes, n_hex: str, e_hex: str) -> str:
    n = int(n_hex, 16)
    e = int(e_hex, 16)
    k = (n.bit_length() + 7) // 8
    max_chunk = k - 11
    out = bytearray()
    for i in range(0, len(data), max_chunk):
        chunk = data[i : i + max_chunk]
        em = _pkcs1_v15_pad(chunk, k)
        m = int.from_bytes(em, "big")
        c = pow(m, e, n)
        out.extend(c.to_bytes(k, "big"))
    return out.hex()


def _utf8_b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _random_scram_nonce_seed() -> str:
    return os.urandom(16).hex() + os.urandom(16).hex()


def _to_b64url_from_hex(hex_str: str) -> str:
    b = bytes.fromhex(hex_str)
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _rsa_encrypt_hex_chunks_oaep_node(data: bytes, n_hex: str, e_hex: str) -> str:
    # Utilise Node.js crypto pour coller à encryptOAEP côté front.
    node_script = r"""
const crypto = require('crypto');
const n = process.argv[1];
const e = process.argv[2];
const b64 = process.argv[3];
const b64url = (h) => Buffer.from(h, 'hex').toString('base64').replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
const key = crypto.createPublicKey({key:{kty:'RSA', n:b64url(n), e:b64url(e)}, format:'jwk'});
const data = Buffer.from(b64, 'base64');
const maxChunk = 214; // même taille que main.js quand rsapadingtype=1
let out = Buffer.alloc(0);
for (let i = 0; i < data.length; i += maxChunk) {
  const chunk = data.subarray(i, i + maxChunk);
  const enc = crypto.publicEncrypt(
    { key, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha1' },
    chunk
  );
  out = Buffer.concat([out, enc]);
}
process.stdout.write(out.toString('hex'));
"""
    b64_payload = base64.b64encode(data).decode("ascii")
    completed = subprocess.run(
        ["node", "-e", node_script, n_hex, e_hex, b64_payload],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Node OAEP encryption failed: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def _post_profiles_with_modes(
    client: httpx.Client,
    token: str,
    n_hex: str,
    e_hex: str,
    rsa_padding_type: int,
    xml_candidates: list[str],
) -> bool:
    # Une seule tentative déterministe (sans retries multi-modes).
    # Flux choisi: UTF-8 -> Base64 -> RSA -> body form-urlencoded "<cipher>=".
    xml_body = xml_candidates[0]
    b64_payload = _utf8_b64(xml_body)
    if rsa_padding_type == 1:
        enc_hex = _rsa_encrypt_hex_chunks_oaep_node(
            b64_payload.encode("ascii"), n_hex, e_hex
        )
    else:
        enc_hex = _rsa_encrypt_hex_chunks(b64_payload.encode("ascii"), n_hex, e_hex)
    payload = bytes.fromhex(enc_hex) + b"="
    try:
        r = client.post(
            PROFILE_API,
            headers={
                **_headers(token),
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8;enc",
            },
            content=payload,
            timeout=30.0,
        )
        if r.status_code == 200 and "<response>OK</response>" in r.text:
            print("[RESET-API] POST profiles OK")
            return True
        return False
    except Exception as e:
        print(f"[RESET-API] POST profiles rejeté: {e}")
        return False


def _api_reset_attempt(proxy_port: int) -> bool:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    with httpx.Client(proxy=proxy_url, timeout=20.0, http2=False) as client:
        ses, token = _get_session_and_token(client)

    def _single_post(url: str, xml_body: str, tok: str) -> httpx.Response:
        headers = {
            **_headers(tok),
            "Content-Type": "application/xml",
            "Connection": "close",
            "Cookie": ses or "",
        }
        with httpx.Client(proxy=proxy_url, timeout=30.0, http2=False) as c:
            return c.post(url, headers=headers, content=xml_body, timeout=30.0)

    def post_ok(url: str, xml_body: str) -> tuple[bool, str]:
        nonlocal token
        try:
            r = _single_post(url, xml_body, token)
        except Exception as e:
            return False, f"transport:{e}"
        token = _latest_token_from_response(r, token)
        if r.status_code == 200 and "<response>OK</response>" in r.text:
            return True, "ok"
        return False, r.text[:120]

    # Strategie A: toggle data switch
    ok_off, msg_off = post_ok(
        DATASWITCH_API, "<request><dataswitch>0</dataswitch></request>"
    )
    if ok_off:
        _wait_modem_disconnected(proxy_port, timeout_s=35)
        time.sleep(6.0)
        ok_on, msg_on = post_ok(
            DATASWITCH_API, "<request><dataswitch>1</dataswitch></request>"
        )
        if ok_on:
            return True
        print(f"[RESET-API] dataswitch ON refusé: {msg_on}")
    else:
        print(f"[RESET-API] dataswitch OFF refusé: {msg_off}")

    # Strategie B: dial down/up (certains firmwares n'appliquent pas dataswitch)
    ok_down, msg_down = post_ok(DIAL_API, "<request><Action>0</Action></request>")
    if ok_down:
        _wait_modem_disconnected(proxy_port, timeout_s=35)
        time.sleep(6.0)
        ok_up, msg_up = post_ok(DIAL_API, "<request><Action>1</Action></request>")
        if ok_up:
            return True
        print(f"[RESET-API] dial up refusé: {msg_up}")
    else:
        print(f"[RESET-API] dial down refusé: {msg_down}")

    # Strategie C: combiner dataswitch puis dial up
    ok_ds0, _ = post_ok(DATASWITCH_API, "<request><dataswitch>0</dataswitch></request>")
    if ok_ds0:
        _wait_modem_disconnected(proxy_port, timeout_s=35)
        time.sleep(4.0)
        ok_ds1, _ = post_ok(DATASWITCH_API, "<request><dataswitch>1</dataswitch></request>")
        if ok_ds1:
            time.sleep(2.0)
            ok_up2, _ = post_ok(DIAL_API, "<request><Action>1</Action></request>")
            if ok_up2:
                return True

    return False


def _wait_modem_reconnected(proxy_port: int, timeout_s: int = 90) -> bool:
    """
    Attends que le modem soit revenu online après le toggle dataswitch.
    Utile quand plusieurs resets sont lancés en parallèle.
    """
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with httpx.Client(proxy=proxy_url, timeout=12.0, http2=False) as client:
                sw = client.get(
                    f"{MODEM_BASE}/api/dialup/mobile-dataswitch",
                    headers={"Connection": "close"},
                    timeout=10.0,
                )
                st = client.get(
                    MONITOR_STATUS_API,
                    headers={"Connection": "close"},
                    timeout=10.0,
                )
            sw_root = ET.fromstring(sw.text)
            st_root = ET.fromstring(st.text)
            data_switch = _xml_text(sw_root, "dataswitch", "")
            conn_status = _xml_text(st_root, "ConnectionStatus", "")
            service_status = _xml_text(st_root, "ServiceStatus", "")
            # 901 + service 2 = état online observé sur tes logs/firmware Huawei.
            if data_switch == "1" and conn_status == "901" and service_status == "2":
                return True
        except Exception:
            pass
        time.sleep(3.0)
    return False


def _wait_modem_disconnected(proxy_port: int, timeout_s: int = 35) -> bool:
    """
    Attends un état offline réel après une commande de coupure.
    Sans ça, certains firmwares reconnectent trop vite et gardent la même IP.
    """
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with httpx.Client(proxy=proxy_url, timeout=12.0, http2=False) as client:
                st = client.get(
                    MONITOR_STATUS_API,
                    headers={"Connection": "close"},
                    timeout=10.0,
                )
            st_root = ET.fromstring(st.text)
            conn_status = _xml_text(st_root, "ConnectionStatus", "")
            service_status = _xml_text(st_root, "ServiceStatus", "")
            if conn_status != "901" or service_status != "2":
                return True
        except Exception:
            pass
        time.sleep(2.0)
    return False


def reset_modem_by_port(proxy_port: int) -> bool:
    print(f"[RESET-API] Démarrage reset API port {proxy_port}...")
    old_ip = _get_modem_ip(proxy_port)
    if old_ip:
        print(f"[RESET-API] IP avant reset: {old_ip}")

    try:
        ok = _api_reset_attempt(proxy_port)
    except Exception as e:
        print(f"[RESET-API] Echec appel API: {e}")
        return False

    if not ok:
        print("[RESET-API] Le modem a refusé la requête de reset API.")
        return False

    if not _wait_modem_reconnected(proxy_port, timeout_s=90):
        print("[RESET-API] ⚠️ Modem pas encore revenu online (timeout monitoring).")

    max_attempts = 24
    for attempt in range(1, max_attempts + 1):
        time.sleep(4)
        new_ip = _get_modem_ip(proxy_port)
        if not new_ip:
            print(f"[RESET-API] Tentative {attempt}/{max_attempts}: IP introuvable")
            continue
        if old_ip is None:
            print(f"[RESET-API] IP après reset: {new_ip}")
            return True
        if new_ip != old_ip:
            print(f"[RESET-API] IP changée: {old_ip} -> {new_ip}")
            return True
        print(
            f"[RESET-API] Tentative {attempt}/{max_attempts}: IP inchangée ({new_ip})"
        )

    print("[RESET-API] IP inchangée après reset.")
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python reset.py <proxy_port>")
        sys.exit(1)
    port = int(sys.argv[1])
    success = reset_modem_by_port(port)
    sys.exit(0 if success else 1)
