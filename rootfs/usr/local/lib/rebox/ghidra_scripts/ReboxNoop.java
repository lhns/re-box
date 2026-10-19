// Does nothing: analyzeHeadless needs a script or an import to create an empty project (supervisor.py).
import ghidra.app.script.GhidraScript;

public class ReboxNoop extends GhidraScript {
    public void run() {}
}
