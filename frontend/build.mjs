import { copyFile, mkdir } from "node:fs/promises";
const target = new URL("../src/ctlrm/web/static/vendor/", import.meta.url);
await mkdir(target, { recursive: true });
for (const [from, to] of [
  ["@xterm/xterm/lib/xterm.js", "xterm.js"],
  ["@xterm/xterm/css/xterm.css", "xterm.css"],
  ["@xterm/xterm/LICENSE", "xterm.LICENSE"],
  ["@xterm/addon-fit/lib/addon-fit.js", "addon-fit.js"],
  ["@xterm/addon-fit/LICENSE", "addon-fit.LICENSE"],
])
  await copyFile(
    new URL(`node_modules/${from}`, import.meta.url),
    new URL(to, target),
  );
