// Stands in for REA's Ghidra JVM (spawned by the patched launcher, see patch-rea.py): imports the
// target into the shared project through ghidra-mcp, with the loader options and pre/post-scripts
// REA's AnalyzeHeadless command line asked for, starts REA's bridge inside the ghidra-mcp JVM
// (ReboxReaAttach.java), then lives as long as that bridge. REA owns this process: killing it
// ends the bridge (the host script's watchdog), and its exit tells REA the provider is gone.
//
// argv: --target <original path> <the AnalyzeHeadless arguments REA built>
// env:  REBOX_GHIDRA_URL (default http://127.0.0.1:8089), REBOX_REA_GHIDRA_WAIT (seconds to wait
//       for the JVM, default 600: the supervisor starts it with the session, and REA's own startup
//       deadline, REA_GHIDRA_STARTUP_TIMEOUT_MS, bounds the whole open anyway),
//       REBOX_REA_BRIDGE_DIR, REBOX_REA_IMPORT_SCRIPTS, REBOX_REA_ATTACH_SCRIPT
import { request } from "node:http";
import { connect } from "node:net";
import { appendFileSync, existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";

const base = new URL(process.env.REBOX_GHIDRA_URL ?? "http://127.0.0.1:8089");
const bridgeDir = process.env.REBOX_REA_BRIDGE_DIR ?? "/opt/rebox/rea-attach/ghidra_scripts";
// REA's pre- and post-scripts: ghidra-mcp runs import scripts only from here (patch-rea.py).
const importScripts = process.env.REBOX_REA_IMPORT_SCRIPTS ?? "/opt/rebox/rea-attach/import_scripts";
const hostScript = process.env.REBOX_REA_ATTACH_SCRIPT ?? "ReboxReaAttach.java";
const startWait = Number(process.env.REBOX_REA_GHIDRA_WAIT || 600);
const BRIDGE = "ReaGhidraBridge.java";

const die = (msg) => {
	process.stderr.write(`re-box attach: ${msg}\n`);
	process.exit(1);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * REA's AnalyzeHeadless arguments (GhidraLauncher.js ghidraHeadlessArguments) as an import:
 * <project root> <project name> -import <file> [-loader X] [-loader-<name> V]... [-processor L]
 * [-cspec C] -readOnly -deleteProject -log P -scriptlog P -scriptPath D
 * [-preScript S args...]... [-postScript S args...]... -postScript ReaGhidraBridge.java <descriptor>.
 * Anything else is refused: a new REA option must be mapped here, not silently dropped.
 */
export const parseHeadless = (argv) => {
	const spec = { loaderArgs: {}, preScripts: [], postScripts: [] };
	const value = (i, flag) => {
		if (i >= argv.length) throw new Error(`${flag} needs a value`);
		return argv[i];
	};
	if (argv.length < 2 || argv[0].startsWith("-") || argv[1].startsWith("-"))
		throw new Error("expected <project root> <project name> first");
	for (let i = 2; i < argv.length; i++) {
		const flag = argv[i];
		if (flag === "-import") spec.file = value(++i, flag);
		else if (flag === "-loader") spec.loader = value(++i, flag);
		else if (flag.startsWith("-loader-")) spec.loaderArgs[flag.slice("-loader-".length)] = value(++i, flag);
		else if (flag === "-processor") spec.language = value(++i, flag);
		else if (flag === "-cspec") spec.compilerSpec = value(++i, flag);
		else if (flag === "-log" || flag === "-scriptPath") value(++i, flag);
		else if (flag === "-scriptlog") spec.scriptLog = value(++i, flag);
		else if (flag === "-readOnly" || flag === "-deleteProject") continue;
		else if (flag === "-preScript" || flag === "-postScript") {
			const path = value(++i, flag);
			const args = [];
			while (i + 1 < argv.length && !argv[i + 1].startsWith("-")) args.push(argv[++i]);
			(flag === "-preScript" ? spec.preScripts : spec.postScripts).push({ path, args });
		}
		else throw new Error(`unsupported AnalyzeHeadless argument ${JSON.stringify(flag)}`);
	}
	const bridge = spec.postScripts.pop();
	if (!spec.file || !spec.scriptLog || bridge === undefined || basename(bridge.path) !== BRIDGE || bridge.args.length !== 1)
		throw new Error(`expected -import, -scriptlog and a final -postScript ${BRIDGE} <descriptor>`);
	spec.descriptor = bridge.args[0];
	return spec;
};

/** REA's import scripts by name, from re-box's copy (patch-rea.py). */
const localScript = ({ path, args }) => {
	const local = join(importScripts, basename(path));
	if (!existsSync(local)) throw new Error(`no ${basename(path)} in ${importScripts}`);
	return { path: local, args };
};

// A lock per program, shared by every helper of this container (same uid). TMPDIR is REA's
// private per-session directory here, so the locks live at a fixed path. A lock whose holder died
// is taken over.
const LOCKS = "/tmp/rebox-attach-locks";
const lockName = async (name) => {
	mkdirSync(LOCKS, { recursive: true, mode: 0o700 });
	const path = join(LOCKS, `${encodeURIComponent(name)}.lock`);
	for (;;) {
		try {
			writeFileSync(path, String(process.pid), { flag: "wx" });
			return () => rmSync(path, { force: true });
		} catch (e) {
			if (e.code !== "EEXIST") throw e;
		}
		let holder, age;
		try {
			holder = Number(readFileSync(path, "utf8"));
			age = Date.now() - statSync(path).mtimeMs;
		} catch {
			continue;  // released meanwhile
		}
		let alive = true;
		if (holder > 0) {
			try {
				process.kill(holder, 0);
			} catch {
				alive = false;
			}
		} else {
			alive = age < 10_000;  // created, its pid not written yet (or a holder that died then)
		}
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
				reject(new Error(`${path}: HTTP ${res.statusCode} ${text.slice(0, 4000)}`));
			else resolve(json);
		});
	});
	req.on("error", reject);
	req.end(data);
});

const main = async () => {
	const args = process.argv.slice(2);
	if (args[0] !== "--target" || args.length < 4) die("usage: --target <path> <AnalyzeHeadless args>");
	const target = args[1];
	let spec;
	try {
		spec = parseHeadless(args.slice(2));
		spec.preScripts = spec.preScripts.map(localScript);
		spec.postScripts = spec.postScripts.map(localScript);
	} catch (e) {
		die(e.message);
	}
	const descriptor = JSON.parse(readFileSync(spec.descriptor, "utf8"));
	// REA's analysis extensions are jars the bridge loads; in the shared JVM they would run with
	// every session's programs. Not supported attached.
	if ((descriptor.analysis_extensions ?? []).length > 0)
		die("REA analysis extensions (REA_GHIDRA_NATIVEAOT_JAR) are not supported in re-box's shared Ghidra");
	// /run_ghidra_script splits its args on whitespace.
	if (/\s/u.test(spec.descriptor + spec.scriptLog + bridgeDir))
		die("the descriptor, script log and bridge paths must contain no whitespace");

	// With the on-demand lifecycle the JVM may still be starting: wait for it and its project.
	const deadline = Date.now() + startWait * 1000;
	for (;;) {
		try {
			if ((await call("/get_project_info")).has_project) break;
		} catch { /* not up yet */ }
		if (Date.now() > deadline) die(`ghidra-mcp not ready at ${base.origin} after ${startWait} s`);
		await sleep(1000);
	}

	// REA's own folder per target and profile: the profile decides loader, scripts and options,
	// so the agent's own import of the same file is never reused. A program already there was
	// imported from the same bytes (import_file checks) with the same profile: reused as is.
	const importTarget = async () => {
		const body = {
			file_path: spec.file,  // REA's verified snapshot of the target
			project_folder: `/rea/${descriptor.target_sha256.slice(0, 12)}-${descriptor.profile_digest.slice(0, 8)}`,
			program_name: basename(target),
			loader: spec.loader ?? "auto",
			auto_analyze: true,
			if_exists: "reuse",
			...Object.keys(spec.loaderArgs).length === 0 ? {} : { loader_args: spec.loaderArgs },
			...spec.language === undefined ? {} : { language: spec.language },
			...spec.compilerSpec === undefined ? {} : { compiler_spec: spec.compilerSpec },
			...spec.preScripts.length === 0 ? {} : { pre_scripts: spec.preScripts },
			...spec.postScripts.length === 0 ? {} : { post_scripts: spec.postScripts },
		};
		// Two sessions opening the same new target at once must end up in one program: ghidra-mcp
		// serializes imports per destination (patch 0002), and so do the helpers, which keeps this
		// true against an unpatched ghidra-mcp too (that saved the second import as "<name>.0").
		const release = await lockName(`${body.project_folder}/${body.program_name}`);
		let imported;
		try {
			imported = await call("/import_file", body);
		} finally {
			release();
		}
		if (imported.script_output) appendFileSync(spec.scriptLog, imported.script_output);
		if (imported.path !== `${body.project_folder}/${body.program_name}`)
			throw new Error(`import_file gave ${imported.path}, not ${body.project_folder}/${body.program_name}`);
		return imported.path;
	};

	let program;
	try {
		program = await importTarget();
		const body = {
			script_name: hostScript,
			args: [spec.descriptor, bridgeDir, process.pid, spec.scriptLog, descriptor.run_id,
				descriptor.target_sha256].join(" "),
			program,
			timeout_seconds: 1800,
		};
		// Ghidra guards script bundle builds with a file lock meant for separate JVMs: two attaches
		// at once met OverlappingFileLockException. Patch 0002 serializes script loads; this retry
		// keeps attaching working against a ghidra-mcp without it.
		for (let attempt = 1; ; attempt++) {
			try {
				await call("/run_ghidra_script", body);
				break;
			} catch (e) {
				if (!e.message.includes("OverlappingFileLockException") || attempt >= 30) throw e;
				await sleep(500 + Math.random() * 1000);
			}
		}
	} catch (e) {
		die(e.message);
	}
	process.stderr.write(`re-box attach: REA bridge ${descriptor.run_id} serving ${program}\n`);

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
};

if (import.meta.url === `file://${process.argv[1]}`) await main();
