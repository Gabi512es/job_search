import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import { GoogleSignInButton } from "@/components/GoogleSignInButton";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "JobScout — Recherche d'emploi assistée par IA" },
      {
        name: "description",
        content:
          "JobScout prépare votre recherche d'emploi : déposez votre CV, indiquez vos critères, nous trouvons les bonnes offres.",
      },
      { property: "og:title", content: "JobScout — Recherche d'emploi assistée par IA" },
      {
        property: "og:description",
        content: "Déposez votre CV et précisez vos critères pour lancer votre recherche.",
      },
    ],
  }),
  component: Index,
});

function Index() {
  const navigate = useNavigate({ from: "/" });
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      if (data.session) {
        navigate({ to: "/onboarding", replace: true });
      } else {
        setChecking(false);
      }
    });
  }, [navigate]);

  if (checking) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background px-4 py-12">
        <p className="text-sm text-muted-foreground">Chargement…</p>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen flex-col items-center justify-center bg-background px-4 py-12 text-center">
      <div className="max-w-xl space-y-8">
        <div className="space-y-4">
          <h1 className="text-4xl font-bold tracking-tight text-foreground sm:text-5xl">
            JobScout
          </h1>
          <p className="text-lg text-muted-foreground sm:text-xl">
            Votre recherche d'emploi, pilotée par l'IA.
          </p>
        </div>

        <div className="flex flex-col items-center gap-4">
          <GoogleSignInButton label="Se connecter avec Google" />
          <p className="max-w-xs text-xs text-muted-foreground">
            Connectez-vous pour déposer votre CV et lancer votre recherche. Vos données restent
            privées.
          </p>
        </div>
      </div>
    </main>
  );
}

