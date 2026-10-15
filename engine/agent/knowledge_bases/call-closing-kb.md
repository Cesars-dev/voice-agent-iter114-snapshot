# Call Closing Knowledge Base

> Retrieved when: the call is ending (after a booking, or when wrapping up).
> KB name: call-closing-kb

---

## Ending the call (after a booking)

1. Recap + SMS: <Thanks, {{first_name}}! You'll receive an SMS text with the confirmation details — you can add it to your calendar in one click, or reschedule if you need to.>
   (Cal.com sends the SMS automatically — you only tell them it's coming; you do not call any SMS function.)
2. Check for more: <Is there anything else I can help you with?> [wait]
3. Warm goodbye: <Really glad you called. Jay's looking forward to it. Have a great day!>
4. Call the `end_call` function.

Rules:
- Do NOT read out any tool data, variables, or JSON.
- Only end when you're certain every question is answered.
- Keep it warm and brief.

---

## Ending the call (no booking / wrap-up)

Use this when the prospect isn't booking (e.g. they asked for a callback, or were disqualified):
1. State what happens next plainly (e.g. <Jay will reach out to you at the number you're calling from.>).
2. <Is there anything else before I let you go?> [wait]
3. Warm goodbye, then call the `end_call` function.
