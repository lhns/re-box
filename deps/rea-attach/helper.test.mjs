// The helper understands every AnalyzeHeadless command line the pinned REA builds: each target
// profile goes through REA's own ghidraHeadlessArguments and back through parseHeadless. A REA
// release that adds an option fails this in the image build instead of at a session's open.
//
//   REA_PACKAGE=/opt/rea/node_modules/rea-agents node --test helper.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { parseHeadless } from "./helper.mjs";

const pkg = process.env.REA_PACKAGE;
const { ghidraHeadlessArguments } = await import(`${pkg}/dist/ghidra/GhidraLauncher.js`);
const { GHIDRA_BRIDGE_SOURCES } = await import(`${pkg}/dist/ghidra/GhidraBridgeSources.js`);

const base = {
	platform: "linux",
	projectRoot: "/tmp/r/project",
	targetPath: "/tmp/r/target-abc.bin",
	bridgeScriptPath: "/rea/bridge/ghidra/ReaGhidraBridge.java",
	descriptorPath: "/tmp/r/session.json",
	ghidraLogPath: "/tmp/r/ghidra.log",
	scriptLogPath: "/tmp/r/script.log",
};
const parse = (extra) => parseHeadless(ghidraHeadlessArguments({ ...base, ...extra }));
const names = (scripts) => scripts.map((s) => [s.path.split("/").at(-1), s.args]);
const noReturn = [GHIDRA_BRIDGE_SOURCES.noReturnFix, []];

test("plain target: auto-detect, the no-return fix, the bridge", () => {
	const spec = parse({});
	assert.equal(spec.file, base.targetPath);
	assert.equal(spec.loader, undefined);
	assert.deepEqual(spec.loaderArgs, {});
	assert.deepEqual(names(spec.preScripts), []);
	assert.deepEqual(names(spec.postScripts), [noReturn]);
	assert.equal(spec.descriptor, base.descriptorPath);
	assert.equal(spec.scriptLog, base.scriptLogPath);
});

test("raw binary: BinaryLoader at the entry address, prepared by a pre-script", () => {
	const spec = parse({ rawBinary: { languageId: "x86:LE:64:default", compilerSpecId: "gcc", baseAddr: "0x401000" } });
	assert.equal(spec.loader, "BinaryLoader");
	assert.deepEqual(spec.loaderArgs, { baseAddr: "0x401000" });
	assert.equal(spec.language, "x86:LE:64:default");
	assert.equal(spec.compilerSpec, "gcc");
	assert.deepEqual(names(spec.preScripts),
		[[GHIDRA_BRIDGE_SOURCES.prepareRaw, ["x86:LE:64:default", "0x401000"]]]);
});

test("DOS COM and MZ", () => {
	const com = parse({ dosCom: true });
	assert.equal(com.loader, "BinaryLoader");
	assert.deepEqual(com.loaderArgs, { baseAddr: "1000:0100" });
	assert.equal(com.language, "x86:LE:16:Real Mode");
	assert.deepEqual(names(com.preScripts), [[GHIDRA_BRIDGE_SOURCES.prepareCom, []]]);
	const mz = parse({ dosMz: true });
	assert.equal(mz.loader, "MzLoader");
	assert.equal(mz.language, "x86:LE:16:Real Mode");
	assert.equal(mz.compilerSpec, "default");
});

test("language override keeps the auto-detected loader", () => {
	const spec = parse({ languageOverride: { languageId: "MIPS:LE:32:default", compilerSpecId: "default" } });
	assert.equal(spec.loader, undefined);
	assert.equal(spec.language, "MIPS:LE:32:default");
	assert.equal(spec.compilerSpec, "default");
	assert.equal(parse({ languageOverride: { languageId: "ARM:LE:32:v8" } }).compilerSpec, undefined);
});

test("seeds, disabled analyzers and MIPS soft-float scripts in REA's order", () => {
	const spec = parse({
		seedPath: "/tmp/r/seeds.json",
		disabledAnalyzers: ["Demangler GNU", "Stack"],
		mipsSoftFloat: true,
	});
	assert.deepEqual(names(spec.preScripts), [
		[GHIDRA_BRIDGE_SOURCES.applySeeds, ["/tmp/r/seeds.json"]],
		[GHIDRA_BRIDGE_SOURCES.analysisOptions, ["Demangler GNU,Stack"]],
	]);
	assert.deepEqual(names(spec.postScripts), [noReturn, [GHIDRA_BRIDGE_SOURCES.mipsSoftFloat, []]]);
});

test("PSP: its loader and processor", () => {
	const spec = parse({ psp: true });
	assert.equal(spec.loader, "PspElfLoader");
	assert.equal(spec.language, "Allegrex:LE:32:default");
});

test("unknown options and a missing bridge are refused", () => {
	const args = ghidraHeadlessArguments(base);
	assert.throws(() => parseHeadless([...args.slice(0, 2), "-noanalysis", ...args.slice(2)]), /unsupported/u);
	assert.throws(() => parseHeadless(args.slice(0, -3)), /final -postScript/u);
});
