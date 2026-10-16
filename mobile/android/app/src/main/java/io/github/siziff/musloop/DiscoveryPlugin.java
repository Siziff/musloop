package io.github.siziff.musloop;

import android.content.Context;
import android.net.wifi.WifiManager;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.DatagramPacket;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

/**
 * Finds musloop on the local Wi-Fi: broadcasts "MUSLOOP?" on UDP 8803 and
 * collects the answers ({"app":"musloop","port":...,"name":...}) for ~1.5 s.
 * The PC's address is the sender of each answer. Pairing still needs the key
 * from the QR code; discovery only saves typing the address.
 */
@CapacitorPlugin(name = "Discovery")
public class DiscoveryPlugin extends Plugin {

    private static final int PORT = 8803;
    private static final byte[] PROBE = "MUSLOOP?".getBytes(StandardCharsets.US_ASCII);

    @PluginMethod
    public void find(PluginCall call) {
        final int timeoutMs = call.getInt("timeoutMs", 1500);
        new Thread(() -> {
            WifiManager.MulticastLock lock = null;
            Map<String, JSObject> found = new LinkedHashMap<>();
            try {
                WifiManager wifi = (WifiManager) getContext().getApplicationContext()
                        .getSystemService(Context.WIFI_SERVICE);
                if (wifi != null) {        // some phones drop broadcast replies without it
                    lock = wifi.createMulticastLock("musloop-discovery");
                    lock.setReferenceCounted(false);
                    lock.acquire();
                }
                try (DatagramSocket sock = new DatagramSocket()) {
                    sock.setBroadcast(true);
                    sock.setSoTimeout(250);
                    for (InetAddress target : broadcastTargets()) {
                        try {
                            sock.send(new DatagramPacket(PROBE, PROBE.length, target, PORT));
                        } catch (Exception ignored) { }
                    }
                    long until = System.currentTimeMillis() + timeoutMs;
                    byte[] buf = new byte[2048];
                    while (System.currentTimeMillis() < until) {
                        DatagramPacket p = new DatagramPacket(buf, buf.length);
                        try {
                            sock.receive(p);
                        } catch (SocketTimeoutException e) {
                            continue;
                        }
                        try {
                            JSONObject o = new JSONObject(new String(p.getData(), 0, p.getLength(), StandardCharsets.UTF_8));
                            if (!"musloop".equals(o.optString("app"))) continue;
                            String host = p.getAddress().getHostAddress();
                            int port = o.optInt("port", 8801);
                            JSObject item = new JSObject();
                            item.put("host", host);
                            item.put("port", port);
                            item.put("name", o.optString("name", host));
                            item.put("url", "http://" + host + ":" + port);
                            found.put(host + ":" + port, item);
                        } catch (Exception ignored) { }
                    }
                }
                JSObject ret = new JSObject();
                ret.put("servers", new JSArray(found.values()));
                call.resolve(ret);
            } catch (Exception e) {
                call.reject("Discovery failed: " + e.getMessage());
            } finally {
                if (lock != null && lock.isHeld()) lock.release();
            }
        }).start();
    }

    /**
     * Check a PC + key natively: GET {server}/api/remote/ping with the key.
     * Done here and not with fetch() in the WebView, because browsers are
     * rolling out "Local Network Access" blocks for page-to-LAN requests.
     * Resolves {status: 200|403|0, body}.
     */
    @PluginMethod
    public void ping(PluginCall call) {
        final String server = call.getString("server", "");
        final String key = call.getString("key", "");
        new Thread(() -> {
            JSObject ret = new JSObject();
            HttpURLConnection c = null;
            try {
                URL u = new URL(server + "/api/remote/ping");
                if (!"http".equals(u.getProtocol()) && !"https".equals(u.getProtocol())) throw new IllegalArgumentException("bad url");
                c = (HttpURLConnection) u.openConnection();
                c.setConnectTimeout(3500);
                c.setReadTimeout(3500);
                c.setRequestProperty("X-Musloop-Key", key);
                int code = c.getResponseCode();
                InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
                String body = "";
                if (in != null) {
                    ByteArrayOutputStream bo = new ByteArrayOutputStream();
                    byte[] b = new byte[4096];
                    int n, total = 0;
                    while ((n = in.read(b)) > 0 && total < 65536) { bo.write(b, 0, n); total += n; }
                    body = bo.toString("UTF-8");
                }
                ret.put("status", code);
                ret.put("body", body);
            } catch (Exception e) {
                ret.put("status", 0);
                ret.put("body", "");
                ret.put("error", String.valueOf(e.getMessage()));
            } finally {
                if (c != null) c.disconnect();
            }
            call.resolve(ret);
        }).start();
    }

    /** 255.255.255.255 plus the directed broadcast of every IPv4 interface. */
    private static Set<InetAddress> broadcastTargets() {
        Set<InetAddress> out = new LinkedHashSet<>();
        try {
            out.add(InetAddress.getByName("255.255.255.255"));
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    InetAddress b = ia.getBroadcast();
                    if (b != null) out.add(b);
                }
            }
        } catch (Exception ignored) { }
        return out;
    }
}
