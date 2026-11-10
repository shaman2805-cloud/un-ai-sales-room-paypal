# UN AI Sales Room · PayPal Sandbox edition

UN AI Sales Room helps small service studios settle scope changes after a client has already paid. Codex turns a change request into a draft with source quotes, the owner approves the revised scope, and the app uses verified PayPal captures and refunds to calculate what is still owed. The examples and prices are fictional. Payments and refunds use PayPal Sandbox. [Original sales proposal prototype](https://github.com/shaman2805-cloud/un-ai-sales-room)

## Run locally

1. Install Python 3.10 or newer. The owner panel uses only the Python standard library.
2. Sign in to the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/) and select **Apps & Credentials → Sandbox**. Use a Sandbox REST app and a personal Sandbox account for the buyer. [PayPal Sandbox account guide](https://developer.paypal.com/sandbox-testing/accounts)
3. On macOS, open `start-sales-room.command`. If prompted, choose whether to generate the fictional AI draft. In the private owner panel, select **Connect PayPal Sandbox** and paste the Client ID and Secret from the same Sandbox app. The panel verifies the pair with PayPal and keeps it in memory until the local server stops.

On other systems, set `PAYPAL_SANDBOX_CLIENT_ID` and `PAYPAL_SANDBOX_CLIENT_SECRET` in your local environment and run the following command from the project folder.

```sh
python3 -m salesroom serve --port 8788 --open
```

If the browser does not open, use the private link printed by the server. Keep that link and your Sandbox credentials out of recordings and source control. Without the credentials, you can still draft, review and approve proposals.

## Try a payment

1. Select **New lead** and use the fictional example. It includes one landing page and one form for **$150.00 USD**. You can also import an AI draft with the steps below.
2. Run the quote check and approve the current version as the owner.
3. Create a Sandbox checkout. Open it and approve the test payment using the **personal Sandbox buyer account**. PayPal returns you to the same local owner panel.
4. Confirm the Sandbox payment in the panel. When PayPal confirms the matching capture, the panel shows the paid state and its capture ID. The audit trail keeps the order and capture alongside the proposal revision.
5. To try an outdated checkout, create one for a second proposal, then change the scope or budget before confirming payment. The old order can no longer be captured through the app. Review and approve the new version to start a new checkout.

If capture is pending or the response was interrupted, check the same order again in the panel. Proposal changes wait until the order can be reconciled. A recorded completed payment remains tied to its original revision.

## Try a change after payment

1. With the $150 example already captured, open **Review a change request**. Paste the fictional message in [`examples/scope-change.demo.txt`](examples/scope-change.demo.txt). Codex drafts the new service quantities and quotes the source wording. If it needs clarification, the draft stays unapplied.
2. Apply the draft and approve the new revision. The catalog now totals $180. The panel carries forward the confirmed $150, so the new Sandbox checkout is only $30.
3. After that second payment, reduce the quote to one $120 landing page. Review and approve the reduced revision. The panel shows $60 credit and disables collection. Review the amount and choose the Sandbox refund action. The app allocates $30 to the latest capture and $30 to the earlier capture. Confirmed refunds appear in the audit history and bring the net paid amount to $120.

A refund attempt keeps one persistent request ID per source capture. If a response is lost or pending, check the same refund. Only a matching PayPal confirmation changes the displayed balance.

## Draft a lead with AI

Use Codex CLI with an account that already has access. The model drafts a structured brief from the fictional request and demo catalog. The local engine calculates prices, checks the draft and requires a separate owner action for approval. The model receives neither Sandbox credentials nor checkout controls.

```sh
python3 -m salesroom.ai_intake --input examples/lead.demo.txt > /tmp/unai-demo-lead.json
python3 -m salesroom create --event-id demo-codex-intake-1 --input /tmp/unai-demo-lead.json
```

Refresh the owner panel to see the new lead. The optional [Switch prompts](switch/room-instructions.md) cover the intake, scoping and review roles for a shared room. Point the agents to this edition and its USD catalog.

## Verify

```sh
python3 -m unittest discover -s tests -v
```

The checkout and refund tests use a simulated PayPal provider. They check authorization, exact amounts, stale revisions, duplicate requests, and recovery from interrupted captures and refunds. The AI workflow tests check source quotes, owner approval, and protection against a stale draft. To verify PayPal itself, follow the Sandbox buyer flow above and compare the capture and refund references with Sandbox transactions.

The private panel runs on the local machine. It is designed for a single owner operating a demo, with the browser session and its data protected by the local user account.
