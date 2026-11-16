import burp.api.montoya.BurpExtension;
import burp.api.montoya.MontoyaApi;
import burp.api.montoya.http.message.HttpRequestResponse;
import burp.api.montoya.ui.contextmenu.ContextMenuEvent;
import burp.api.montoya.ui.contextmenu.ContextMenuItemsProvider;
import java.awt.Component;

import javax.swing.JMenuItem;
import javax.swing.JOptionPane;
import javax.swing.SwingUtilities;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;

public final class BountyTrackerExtension implements BurpExtension {
    private static final URI RECEIVER = URI.create("http://127.0.0.1:8765/api/burp");

    @Override
    public void initialize(MontoyaApi api) {
        api.extension().setName("BountyTracker Export");
        api.userInterface().registerContextMenuItemsProvider(new ContextMenuItemsProvider() {
            @Override
            public List<Component> provideMenuItems(ContextMenuEvent event) {
                List<HttpRequestResponse> selected = List.copyOf(event.selectedRequestResponses());
                if (selected.isEmpty()) {
                    return List.of();
                }
                JMenuItem item = new JMenuItem("Send selected requests to BountyTracker");
                item.addActionListener(e -> {
                    String id = JOptionPane.showInputDialog("Enter the Program ID shown in BountyTracker Settings:");
                    if (id == null || !id.trim().matches("[1-9][0-9]*")) {
                        return;
                    }
                    new Thread(() -> export(id.trim(), selected), "BountyTrackerExport").start();
                });
                return List.of(item);
            }
        });
        api.logging().logToOutput("BountyTracker Export loaded. Select requests and use the right-click menu.");
    }

    private void export(String id, List<HttpRequestResponse> selected) {
        int accepted = 0;
        int outOfScope = 0;
        int rejected = 0;
        try {
            String local = System.getenv("LOCALAPPDATA");
            if (local == null) {
                throw new IllegalStateException("LOCALAPPDATA is unavailable.");
            }
            String token = Files.readString(Path.of(local, "BountyTracker", "burp_token.txt")).trim();
            HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
            for (HttpRequestResponse record : selected) {
                if (record.request() == null) {
                    rejected++;
                    continue;
                }
                String url = record.request().url().split("[?#]", 2)[0];
                int status = record.response() == null ? 0 : record.response().statusCode();
                String json = "{\"program_id\":" + id + ",\"method\":\"" + escape(record.request().method())
                    + "\",\"url\":\"" + escape(url) + "\",\"status\":" + status + "}";
                var sent = record.timingData().map(timing -> timing.timeRequestSent()).map(time -> time.toInstant().toString());
                if (sent.isPresent()) {
                    json = json.substring(0, json.length() - 1) + ",\"capture_time\":\"" + sent.get() + "\"}";
                }
                HttpRequest request = HttpRequest.newBuilder(RECEIVER)
                    .timeout(Duration.ofSeconds(15))
                    .header("Content-Type", "application/json")
                    .header("X-Bounty-Token", token)
                    .POST(HttpRequest.BodyPublishers.ofString(json)).build();
                int result = client.send(request, HttpResponse.BodyHandlers.discarding()).statusCode();
                switch (result) {
                    case 200 -> accepted++;
                    case 403 -> outOfScope++;
                    default -> rejected++;
                }
            }
            show("Accepted: " + accepted + "\nOutside scope: " + outOfScope + "\nRejected: " + rejected);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
        } catch (Exception error) {
            show("Export stopped after " + accepted + " accepted requests. " + error.getMessage());
        }
    }

    private static void show(String message) {
        SwingUtilities.invokeLater(() -> JOptionPane.showMessageDialog(null, message));
    }

    private static String escape(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t");
    }
}

