#!/usr/bin/env node

if (process.argv.includes("--help")) {
  console.log("Usage: node examples/ws_client.mjs [url] [count]");
  console.log("Minimal client for chn4-bridge serve. Needs Node.js 22+ (built-in WebSocket).");
  process.exit(0);
}

const url = process.argv[2] ?? "ws://127.0.0.1:8765";
const count = process.argv[3] ? Number(process.argv[3]) : Infinity;
const SUPPORTED_SCHEMA = 1;

if (typeof WebSocket === "undefined") {
  console.error("This script needs Node.js 22 or newer (global WebSocket).");
  process.exit(1);
}

const format = (value, digits, unit) => (value === null ? "n/a" : `${value.toFixed(digits)} ${unit}`);

const channelLine = (channel) => {
  if (!channel.present) return `CH${channel.channel}: empty`;
  return [
    `CH${channel.channel}: ${channel.state.padEnd(9)}`,
    format(channel.voltage_v, 3, "V").padStart(9),
    format(channel.current_recent_a, 3, "A~").padStart(10),
    format(channel.capacity_mah, 1, "mAh").padStart(11),
  ].join(" ");
};

let received = 0;
const socket = new WebSocket(url);

socket.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  const data = message.data;
  switch (message.type) {
    case "hello":
      if (data.schema_version !== SUPPORTED_SCHEMA) {
        console.error(`Unsupported schema_version ${data.schema_version}`);
        socket.close();
        return;
      }
      console.log(`Connected to chn4-bridge ${data.server_version}, source: ${data.source.kind} (${data.source.description})`);
      if (data.source.simulated) console.log("SIMULATED DATA: values are synthetic, not measurements.");
      break;
    case "probe_status":
      console.log(`[status] ${data.state}${data.message ? `: ${data.message}` : ""}`);
      break;
    case "error":
      console.log(`[error] ${data.code}: ${data.message}`);
      break;
    case "shutdown":
      console.log(`[shutdown] ${data.reason}`);
      socket.close();
      break;
    case "snapshot":
      console.log(`#${data.sequence} ${data.captured_at} ${data.consistency}${data.stale ? " STALE" : ""}`);
      for (const channel of data.channels) console.log(`  ${channelLine(channel)}`);
      received += 1;
      if (received >= count) socket.close();
      break;
    default:
      break;
  }
});

socket.addEventListener("error", () => {
  console.error(`Connection to ${url} failed. Is "chn4-bridge serve" running?`);
  process.exitCode = 1;
});

socket.addEventListener("close", () => process.exit());

process.on("SIGINT", () => socket.close());
