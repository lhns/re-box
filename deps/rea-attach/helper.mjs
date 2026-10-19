// Stands in for REA's Ghidra JVM (spawned by the patched launcher, patch-rea.py): imports the
// target into the shared project through ghidra-mcp, starts REA's bridge inside the ghidra-mcp
// JVM, then lives until REA stops it. REA owns this process: its exit or kill ends the bridge.
//
// argv: --target <original path> <AnalyzeHeadless arguments REA built>
// env:  REBOX_GHIDRA_URL (default http://127.0.0.1:8089), REBOX_REA_BRIDGE_DIR,
//       REBOX_REA_ATTACH_SCRIPT (host script path), REBOX_GHIDRA_WAIT_SECONDS (default 300)
const base = process.env.REBOX_GHIDRA_URL ?? "http://127.0.0.1:8089";
const bridgeDir = process.env.REBOX_REA_BRIDGE_DIR ?? "/opt/rebox/rea-attach/ghidra_scripts";
const hostScript = process.env.REBOX_REA_ATTACH_SCRIPT ?? "ReboxReaAttach.java";
const waitSeconds = Number(process.env.REBOX_GHIDRA_WAIT_SECONDS ?? 300);

const die = (msg) => {
	process.stderr.write(`re-box attach: ${msg}\n`);
	process.exit(1);
};

const args = process.argv.slice(2);
if (args[0] !== "--target" || args.length < 4) die("usage: --target <path> <AnalyzeHeadless args>");
const target = args[1];
const headless = args.slice(2);
const after = (flag) => {
	const i = headless.indexOf(flag);
	return i < 0 ? undefined : headless[i + 1];
};
const descriptor = headless.at(-1);  // -postScript <bridge> <descriptor> is always last
const scriptLog = after("-scriptlog");
const runId = JSON.parse(await (await import("node:fs/promises")).readFile(descriptor, "utf8")).run_id;
if (!scriptLog || /\s/u.test(descriptor + scriptLog + bridgeDir)) die("paths must be set and contain no whitespace");

const call = async (path, body) => {
	const r = await fetch(base + path, body === undefined ? {} : {
		method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
	});
	const text = await r.text();
	let json;
	try { json = JSON.parse(text); } catch { json = undefined; }
	if (!r.ok || json === undefined || json.success === false || json.error !== undefined)
		throw new Error(`${path}: HTTP ${r.status} ${text.slice(0, 2000)}`);
	return json;
};

// The supervisor starts the JVM on demand; wait for it and its project.
const deadline = Date.now() + waitSeconds * 1000;
for (;;) {
	try {
		if ((await call("/get_project_info")).has_project) break;
	} catch { /* not up yet */ }
	if (Date.now() > deadline) die(`ghidra-mcp not ready at ${base} after ${waitSeconds} s`);
	await new Promise((r) => setTimeout(r, 1000));
}

try {
	// Synchronous: returns after auto-analysis. A file of that name already in the project is
	// reused as is; the bridge's sha check refuses it if it is a different binary.
	const imported = await call("/import_file", { file_path: target, project_folder: "/", auto_analyze: true });
	const program = imported.path;
	if (imported.reused_existing) {
		const status = await call(`/analysis_status?program=${encodeURIComponent(program)}`);
		const entry = (status.programs ?? status.results ?? [status]).find((p) => p.analyzed !== undefined) ?? {};
		if (entry.analyzed === false) await call("/reanalyze", { program });
	}
	const started = await call("/run_ghidra_script", {
		script_name: hostScript,
		args: [descriptor, bridgeDir, process.pid, scriptLog, runId].join(" "),
		program,
		timeout_seconds: 1800,
	});
	process.stderr.write(`re-box attach: ${program}: ${String(started.console_output ?? "").split("\n").filter((l) => l.startsWith("REA bridge")).join(" ")}\n`);
} catch (e) {
	die(e.message);
}

// Live until REA's cleanup kills the process group. If the JVM goes away, so did the bridge:
// exit, and REA reports the provider gone (reopen the binary to attach again).
const { connect } = await import("node:net");
const { hostname, port } = new URL(base);
const alive = () => new Promise((resolve) => {
	const s = connect({ host: hostname, port: Number(port), timeout: 5000 });
	s.once("connect", () => { s.destroy(); resolve(true); });
	s.once("timeout", () => { s.destroy(); resolve(false); });
	s.once("error", () => resolve(false));
});
setInterval(async () => {
	if (!(await alive())) die("ghidra-mcp went away; the REA bridge with it");
}, 2000);
