// Stands in for REA's Ghidra JVM (spawned by the patched launcher, see patch-rea.py): imports the
// target into the shared project through ghidra-mcp, starts REA's bridge inside the ghidra-mcp
// JVM (ReboxReaAttach.java), then lives as long as that bridge. REA owns this process: killing
// it ends the bridge (the host script's watchdog), and its exit tells REA the provider is gone.
//
// argv: --target <original path> <the AnalyzeHeadless arguments REA built>
// env:  REBOX_GHIDRA_URL (default http://127.0.0.1:8089), REBOX_REA_GHIDRA_WAIT (seconds to wait
//       for the JVM, default 600: the supervisor starts it with the session, and REA's own startup
//       deadline, REA_GHIDRA_STARTUP_TIMEOUT_MS, bounds the whole open anyway),
//       REBOX_REA_BRIDGE_DIR, REBOX_REA_ATTACH_SCRIPT
import { request } from "node:http";
import { connect } from "node:net";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";

const base = new URL(process.env.REBOX_GHIDRA_URL ?? "http://127.0.0.1:8089");
const bridgeDir = process.env.REBOX_REA_BRIDGE_DIR ?? "/opt/rebox/rea-attach/ghidra_scripts";
const hostScript = process.env.REBOX_REA_ATTACH_SCRIPT ?? "ReboxReaAttach.java";
const startWait = Number(process.env.REBOX_REA_GHIDRA_WAIT || 600);
const SHA_MISMATCH = "REBOX_SHA_MISMATCH";

const die = (msg) => {
	process.stderr.write(`re-box attach: ${msg}\n`);
	process.exit(1);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const args = process.argv.slice(2);
if (args[0] !== "--target" || args.length < 4) die("usage: --target <path> <AnalyzeHeadless args>");
const target = args[1];
const headless = args.slice(2);
const scriptLogAt = headless.indexOf("-scriptlog");
const scriptLog = scriptLogAt < 0 ? undefined : headless[scriptLogAt + 1];
const descriptorPath = headless.at(-1);  // -postScript <bridge> <descriptor> always comes last
const descriptor = JSON.parse(readFileSync(descriptorPath, "utf8"));
// /run_ghidra_script splits its args on whitespace.
if (!scriptLog || /\s/u.test(descriptorPath + scriptLog + bridgeDir))
	die("the descriptor, script log and bridge paths must be set and contain no whitespace");

// A per-target lock shared by every helper of this container (same uid). TMPDIR is REA's private
// per-session directory here, so the lock lives at a fixed path. A lock whose holder died is taken over.
const LOCKS = "/tmp/rebox-attach-locks";
const lockTarget = async (sha256) => {
	mkdirSync(LOCKS, { recursive: true, mode: 0o700 });
	const path = join(LOCKS, `${sha256}.lock`);
	for (;;) {
		try {
			writeFileSync(path, String(process.pid), { flag: "wx" });
			return () => rmSync(path, { force: true });
		} catch (e) {
			if (e.code !== "EEXIST") throw e;
		}
		const holder = Number(readFileSync(path, { encoding: "utf8", flag: "a+" }));
		let alive = false;
		try {
			process.kill(holder, 0);
			alive = holder > 0;
		} catch { /* gone */ }
		if (!alive) rmSync(path, { force: true });
		else await sleep(250);
	}
};

// node:http, not fetch: fetch gives up on a response after 300 s, and an import with
// auto-analysis answers only when the analysis is done. REA's startup deadline bounds it.
const call = (path, body) => new Promise((resolve, reject) => {
	const data = body === undefined ? undefined : JSON.stringify(body);
	const req = request(new URL(path, base), {
		method: data === undefined ? "GET" : "POST",
		headers: data === undefined ? {} : { "content-type": "application/json", "content-length": Buffer.byteLength(data) },
	}, (res) => {
		let text = "";
		res.setEncoding("utf8");
		res.on("data", (c) => { text += c; });
		res.on("end", () => {
			let json;
			try { json = JSON.parse(text); } catch { /* reported below */ }
			if (res.statusCode !== 200 || json === undefined || json.success === false || json.error !== undefined)
				reject(new Error(`${path}: HTTP ${res.statusCode} ${text.slice(0, 2000)}`));
			else resolve(json);
		});
	});
	req.on("error", reject);
	req.end(data);
});

// With the on-demand lifecycle the JVM may still be starting: wait for it and its project.
const deadline = Date.now() + startWait * 1000;
for (;;) {
	try {
		if ((await call("/get_project_info")).has_project) break;
	} catch { /* not up yet */ }
	if (Date.now() > deadline) die(`ghidra-mcp not ready at ${base.origin} after ${startWait} s`);
	await sleep(1000);
}

// The target, imported and analyzed, as a program path in the project. import_file returns after
// auto-analysis. A file of the same name already in the folder is reused as it is; the host
// script refuses it when its bytes differ from REA's target, and the target then goes to a
// folder of its own. The only place that knows how ghidra-mcp imports.
const importTarget = async (folder) => {
	const body = { file_path: target, project_folder: folder, auto_analyze: true };
	// import_file checks for an existing program, then imports: two sessions opening the same new
	// file at once both import it, and Ghidra saves the second as "<name>.0", a separate program
	// (seen in CI: one session's rename landed in the copy). Take turns per target.
	const release = await lockTarget(descriptor.target_sha256);
	let imported;
	try {
		imported = await call("/import_file", body);
	} finally {
		release();
	}
	const program = imported.path;
	if (basename(program) !== basename(target))
		throw new Error(`import_file gave ${program}, not ${basename(target)}: a concurrent import?`);
	if (imported.reused_existing) {
		const status = await call(`/analysis_status?program=${encodeURIComponent(program)}`);
		const entry = (status.programs ?? [status]).find((p) => p.analyzed !== undefined) ?? {};
		if (entry.analyzed === false) await call("/reanalyze", { program });
	}
	return program;
};

const attach = async (folder) => {
	const program = await importTarget(folder);
	const body = {
		script_name: hostScript,
		args: [descriptorPath, bridgeDir, process.pid, scriptLog, descriptor.run_id, descriptor.target_sha256].join(" "),
		program,
		timeout_seconds: 1800,
	};
	// Ghidra guards script bundle builds with a file lock meant for separate JVMs: two sessions
	// attaching at once in this one JVM get OverlappingFileLockException. Take turns.
	for (let attempt = 1; ; attempt++) {
		try {
			await call("/run_ghidra_script", body);
			return program;
		} catch (e) {
			if (!e.message.includes("OverlappingFileLockException") || attempt >= 30) throw e;
			await sleep(500 + Math.random() * 1000);
		}
	}
};

let program;
try {
	try {
		program = await attach("/");
	} catch (e) {
		if (!e.message.includes(SHA_MISMATCH)) throw e;
		program = await attach(`/rea/${descriptor.target_sha256.slice(0, 12)}`);
	}
} catch (e) {
	die(e.message);
}
process.stderr.write(`re-box attach: REA bridge ${descriptor.run_id} serving ${program} (${basename(target)})\n`);

// Live as long as the bridge: it removes its socket when it ends, and a JVM that went away took
// it along. Either way exit, and REA reports the provider gone (reopen the binary to attach again).
const socket = descriptor.endpoint_path;
const jvmAlive = () => new Promise((resolve) => {
	const s = connect({ host: base.hostname, port: Number(base.port || 80), timeout: 5000 });
	s.once("connect", () => { s.destroy(); resolve(true); });
	s.once("timeout", () => { s.destroy(); resolve(false); });
	s.once("error", () => resolve(false));
});
for (let waited = 0; !existsSync(socket); waited += 200) {
	if (waited > 120_000) die("the bridge never opened its socket");
	await sleep(200);
}
for (;;) {
	await sleep(2000);
	// As REA's own JVM does once its bridge returns (after `shutdown`, or on a lost connection).
	if (!existsSync(socket)) process.exit(0);
	if (!(await jvmAlive())) die("ghidra-mcp went away, and the REA bridge with it");
}
