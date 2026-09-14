import { createFileRoute } from "@tanstack/react-router";
import { GoogleSignInButton } from "@/components/GoogleSignInButton";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export const Route = createFileRoute("/auth")({
  head: () => ({
    meta: [
      { title: "Connexion — Recherche d'emploi assistée par IA" },
      { name: "description", content: "Connectez-vous avec Google pour accéder à votre profil." },
      { property: "og:title", content: "Connexion" },
      { property: "og:description", content: "Connectez-vous avec Google pour continuer." },
    ],
  }),
  component: AuthPage,
});

function AuthPage() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-4 py-12">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-2xl">Connexion</CardTitle>
          <CardDescription>Utilisez votre compte Google pour continuer.</CardDescription>
        </CardHeader>
        <CardContent>
          <GoogleSignInButton />
        </CardContent>
      </Card>
    </main>
  );
}
