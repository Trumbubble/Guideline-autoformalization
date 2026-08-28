// Minimal driver around the official cql-to-elm translator (DEV_PLAN P6).
//
// The translator ships as a library, not a CLI, so this is the thin wrapper:
// read CQL, translate, write ELM JSON, and report errors on stderr in a form
// the Python repair loop can feed back verbatim.
//
// Exit 0 = translated. Exit 1 = translation errors (the interesting case).
// Exit 2 = the driver itself failed, which must NOT be read as a clean compile.
import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import org.cqframework.cql.cql2elm.CqlCompilerException;
import org.cqframework.cql.cql2elm.CqlTranslator;
import org.cqframework.cql.cql2elm.LibraryManager;
import org.cqframework.cql.cql2elm.ModelManager;
import org.cqframework.cql.cql2elm.DefaultLibrarySourceProvider;

public class Translate {
    public static void main(String[] args) {
        if (args.length < 1) {
            System.err.println("usage: Translate <file.cql> [out.json]");
            System.exit(2);
        }
        try {
            File in = new File(args[0]);
            ModelManager modelManager = new ModelManager();
            LibraryManager libraryManager = new LibraryManager(modelManager);
            libraryManager.getLibrarySourceLoader()
                .registerProvider(new DefaultLibrarySourceProvider(in.getAbsoluteFile()
                                                                    .getParentFile().toPath()));
            CqlTranslator translator = CqlTranslator.fromFile(in, libraryManager);

            List<CqlCompilerException> errors = translator.getErrors();
            for (CqlCompilerException e : errors) {
                String where = "";
                if (e.getLocator() != null) {
                    where = " [line " + e.getLocator().getStartLine()
                          + ":" + e.getLocator().getStartChar() + "]";
                }
                System.err.println("ERROR" + where + ": " + e.getMessage());
            }
            if (!errors.isEmpty()) { System.exit(1); }

            String json = translator.toJson();
            if (args.length > 1) { Files.writeString(Path.of(args[1]), json); }
            else { System.out.println(json); }
            System.exit(0);
        } catch (Exception ex) {
            System.err.println("DRIVER-FAILURE: " + ex.getClass().getSimpleName()
                               + ": " + ex.getMessage());
            System.exit(2);
        }
    }
}
