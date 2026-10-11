package typegen

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"

	"github.com/pocketbase/pocketbase"
	"github.com/spf13/cobra"
	_ "modernc.org/sqlite"
)

// Register installs the types command. Call RunIfRequested before app.Start;
// PocketBase otherwise bootstraps its database before executing custom commands.
func Register(app *pocketbase.PocketBase) {
	var schemaPath, output, schemaOutput, packageName string
	var includeSystem, check bool
	command := &cobra.Command{
		Use:   "types",
		Short: "Generate Go schema constants and select enums without database writes",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			var schema []Collection
			var err error
			if schemaPath != "" {
				schema, err = readSchemaFile(schemaPath)
			} else {
				databasePath := filepath.Join(app.DataDir(), "data.db")
				_, statErr := os.Stat(databasePath)
				if errors.Is(statErr, os.ErrNotExist) {
					fmt.Fprintln(cmd.OutOrStdout(), "No local data.db; using the checked-in schema.json. Start the backend to create your local database.")
					schema, err = readSchemaFile("schema.json")
				} else if statErr != nil {
					return statErr
				} else {
					schema, err = ReadDatabase(databasePath)
				}
			}
			if err != nil {
				return err
			}
			schema = Normalize(schema, includeSystem)
			generated, err := Generate(schema, packageName)
			if err != nil {
				return err
			}
			var encodedSchema []byte
			if schemaOutput != "" {
				absoluteOutput, err := filepath.Abs(output)
				if err != nil {
					return err
				}
				absoluteSchemaOutput, err := filepath.Abs(schemaOutput)
				if err != nil {
					return err
				}
				if absoluteOutput == absoluteSchemaOutput {
					return fmt.Errorf("--output and --schema-output must be different files")
				}
				encodedSchema, err = json.MarshalIndent(schema, "", "  ")
				if err != nil {
					return err
				}
				encodedSchema = append(encodedSchema, '\n')
			}
			if err := saveOutput(output, generated, check); err != nil {
				return err
			}
			if schemaOutput != "" {
				if err := saveOutput(schemaOutput, encodedSchema, check); err != nil {
					return err
				}
			}
			if check {
				fmt.Fprintf(cmd.OutOrStdout(), "%s matches the schema (%d collections).\n", output, len(schema))
			} else {
				fmt.Fprintf(cmd.OutOrStdout(), "Generated %s (%d collections).\n", output, len(schema))
			}
			return nil
		},
	}
	command.Flags().StringVar(&schemaPath, "schema", "", "use a PocketBase collections JSON export instead of the local database")
	command.Flags().StringVar(&output, "output", "collections/collections.go", "Go output file")
	command.Flags().StringVar(&schemaOutput, "schema-output", "", "write a sanitized collections JSON snapshot for frontend type generation")
	command.Flags().StringVar(&packageName, "package", "collections", "Go package name")
	command.Flags().BoolVar(&includeSystem, "include-system", false, "include internal PocketBase system collections")
	command.Flags().BoolVar(&check, "check", false, "fail if generated files differ, without writing files")
	app.RootCmd.AddCommand(command)
}

// RunIfRequested executes only the types command directly through Cobra. This
// deliberately bypasses PocketBase.Start and its database bootstrap/migrations.
// The bool tells main to return even when command execution reports an error.
func RunIfRequested(app *pocketbase.PocketBase) (bool, error) {
	command, _, err := app.RootCmd.Find(os.Args[1:])
	if err != nil || command == nil || command.Name() != "types" {
		return false, nil
	}
	return true, app.RootCmd.Execute()
}

func readSchemaFile(path string) ([]Collection, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read schema %s: %w; use --schema with a Dashboard collections JSON export", path, err)
	}
	return DecodeSchema(data)
}

// ReadDatabase reads the _collections table through a read-only SQLite
// connection. It does not start PocketBase, run migrations, or read records.
// SQLite may maintain its WAL shared-memory sidecar for reader coordination.
func ReadDatabase(path string) ([]Collection, error) {
	if _, err := os.Stat(path); err != nil {
		return nil, fmt.Errorf("read database schema: %w", err)
	}
	absolutePath, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	databaseURL := url.URL{Scheme: "file", Path: absolutePath}
	query := databaseURL.Query()
	query.Set("mode", "ro")
	query.Set("_pragma", "busy_timeout(5000)")
	databaseURL.RawQuery = query.Encode()
	database, err := sql.Open("sqlite", databaseURL.String())
	if err != nil {
		return nil, err
	}
	defer database.Close()
	database.SetMaxOpenConns(1)
	rows, err := database.Query(`SELECT id, name, type, system, fields FROM _collections ORDER BY name`)
	if err != nil {
		return nil, fmt.Errorf("read PocketBase schema: %w; for a pre-0.23 database, upgrade it with PocketBase before generating types", err)
	}
	defer rows.Close()
	var schema []Collection
	for rows.Next() {
		var collection Collection
		var fields string
		if err := rows.Scan(&collection.ID, &collection.Name, &collection.Type, &collection.System, &fields); err != nil {
			return nil, err
		}
		if err := json.Unmarshal([]byte(fields), &collection.Fields); err != nil {
			return nil, fmt.Errorf("decode fields for %s: %w", collection.Name, err)
		}
		schema = append(schema, collection)
	}
	return schema, rows.Err()
}

func saveOutput(path string, data []byte, check bool) error {
	existing, err := os.ReadFile(path)
	if err == nil && bytes.Equal(existing, data) {
		return nil
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if check {
		return fmt.Errorf("%s is out of date; run make types", path)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".typegen-*")
	if err != nil {
		return err
	}
	defer os.Remove(temporary.Name())
	if _, err := temporary.Write(data); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Chmod(0o644); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporary.Name(), path)
}
