package main

import (
	"log"
	"os"
	"time"

	"github.com/joho/godotenv"

	"github.com/lsherman98/openx/pocketbase/internal/typegen"
	// _ "github.com/lsherman98/openx/pocketbase/migrations" // Uncomment when the first migration is added.
	"github.com/lsherman98/openx/pocketbase/observability"
	"github.com/pocketbase/pocketbase"
	"github.com/pocketbase/pocketbase/apis"
	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/plugins/migratecmd"
)

func main() {
	app := pocketbase.New()
	var publicDir string
	app.RootCmd.PersistentFlags().StringVar(&publicDir, "publicDir", "./pb_public", "directory containing the frontend build")
	typegen.Register(app)
	if handled, err := typegen.RunIfRequested(app); handled {
		if err != nil {
			log.Fatal(err)
		}
		return
	}

	if err := godotenv.Load(); err != nil && !os.IsNotExist(err) {
		log.Fatal("Read .env: ", err)
	}

	config, err := observability.ConfigFromEnv()
	if err != nil {
		log.Fatal(err)
	}
	if err := observability.Register(app, config); err != nil {
		log.Fatal(err)
	}
	app.OnServe().BindFunc(func(se *core.ServeEvent) error {
		se.Router.GET("/{path...}", apis.Static(os.DirFS(publicDir), true))
		return se.Next()
	})

	migratecmd.MustRegister(app, app.RootCmd, migratecmd.Config{
		Automigrate: app.IsDev() && config.Environment != "production",
	})

	if err := app.Start(); err != nil {
		observability.CaptureError(app, err)
		observability.Flush(app, 2*time.Second)
		log.Fatal(err)
	}
}
