import { useState } from "react";
import { toast } from "sonner";
import { lovable } from "@/integrations/lovable/index";
import { Button } from "@/components/ui/button";

export function GoogleSignInButton({ label = "Continuer avec Google" }: { label?: string }) {
  const [loading, setLoading] = useState(false);

  const handleClick = async () => {
    setLoading(true);
    const result = await lovable.auth.signInWithOAuth("google", {
      redirect_uri: window.location.origin,
    });

    if (result.error) {
      setLoading(false);
      toast.error("La connexion a échoué. Merci de réessayer.");
      return;
    }

    if (result.redirected) return;

    window.location.href = "/onboarding";
  };

  return (
    <Button size="lg" className="w-full" onClick={handleClick} disabled={loading}>
      {loading ? "Connexion…" : label}
    </Button>
  );
}
