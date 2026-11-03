---
name: grocery-list
description: Turn recipes or a menu into a shopping list grouped by grocery store department (produce, bakery, deli, butcher, seafood, dairy, canned goods, dry goods, baking, spices, frozen...) in walking order, with duplicate ingredients merged, then print it on the thermal receipt printer. Takes a recipe or menu URL, pasted recipe text, or several of them at once. TRIGGER when the user asks for a shopping or grocery list from a recipe, menu, meal plan or link, asks "what do I need to buy for...", or wants a recipe's ingredients printed.
allowed-tools: Bash(print-to-thermal *)
---

# Grocery list from recipes

Read the recipes, build one merged list, sort it by store department, check it
with the user, then print it with `print-to-thermal`. The `thermal` skill
covers the printer itself. If `print-to-thermal config` exits with code 3, the
printer is not set up: follow that skill's first-run setup before printing.

## 1. Get the ingredients

- **Recipe URL:** fetch it. The skill doesn't pre-approve web fetches, so
  the user approves each one when prompted. Most recipe sites embed
  `schema.org/Recipe` JSON-LD, and its `recipeIngredient` array is the most
  reliable ingredient list, so look for that first and use the page text only
  if it's missing.
  Also note `name` (the dish name) and `recipeYield`.
- **Menu URL or meal plan** (a list of dishes, not recipes): work out a
  typical ingredient list for each dish. Tell the user these are your
  assumptions, not quoted from the page, and invite corrections.
- **Pasted text:** use it as given.
- **Several sources:** handle each one, remembering which dish each
  ingredient came from.
- If the user declines the fetch, or a page refuses it, is paywalled or needs
  a login, ask the user to paste the recipe. Don't retry it or try other
  routes to the same page.

If the user asks for more or fewer servings, scale the amounts before merging.

## 2. Normalize and merge

Turn every line into `{name, qty}` for **buying**, not cooking:

- Drop prep words: "finely diced", "divided", "softened", "plus more for
  serving", "at room temperature". Keep words that change what you buy:
  "boneless skinless", "low-sodium", "full-fat", "fresh" vs "dried".
- Use the plain item name a shopper looks for: "Yellow onions", not "1 medium
  yellow onion, halved".
- Turn cooking amounts into buying amounts where it's obvious: 3 cloves garlic
  becomes "1 head", juice of 2 limes becomes "2" limes, 1/4 cup chopped
  cilantro becomes "1 bunch", 2 cups cooked rice becomes the dry rice.
- **Merge duplicates** across recipes. Add the amounts when the units match
  (2 + 1 onions = 3). When they don't, list both ("1 cup + 2 tbsp"). Don't
  convert between volume and weight.
- **Every item gets a qty.** If none applies, use a count ("1"). A blank qty
  prints a line of dots that ends in nothing.
- **Staples** (salt, pepper, cooking oil, common dried spices, flour, sugar)
  go in a final **Check pantry** department with qty `have?`, so the user
  checks the cupboard instead of buying a duplicate. Leave out water and ice.

## 3. Sort by department

Use these departments in this order, which is roughly how a store is walked.
Leave out any with nothing in them.

| Department | Goes here |
|---|---|
| Produce | fresh fruit and vegetables, fresh herbs, garlic, ginger, fresh chiles, tofu if the store keeps it there |
| Bakery | bread, rolls, buns, tortillas, pita, naan |
| Deli | sliced meats, deli-counter cheese, olives, prepared salads |
| Butcher | beef, pork, lamb, poultry, ground meat, sausage, bacon |
| Seafood | fish, shrimp, shellfish |
| Dairy & Eggs | milk, cream, butter, eggs, yogurt, sour cream, packaged cheese |
| Canned & Jarred | canned tomatoes, beans, stock and broth, coconut milk, pickles, jarred sauces |
| Dry Goods & Pasta | pasta, rice, grains, lentils, dried beans, breadcrumbs, cereal, nuts |
| Baking | flour, sugars, baking soda and powder, yeast, chocolate, vanilla |
| Spices & Condiments | dried spices and blends, oils, vinegars, soy sauce, mustard, ketchup, honey |
| International | fish sauce, gochujang, curry paste, tahini, harissa, specialty items |
| Frozen | frozen vegetables and fruit, puff pastry, ice cream |
| Beverages | drinks, wine or beer used in cooking |
| Check pantry | staples, from step 2 |

Rules for the borderline items:

- Fresh herbs go in Produce; dried herbs go in Spices & Condiments.
- Parmesan, feta and other packaged cheese go in Dairy & Eggs. Only cheese
  "sliced at the counter" goes in Deli.
- Anything described as "frozen" goes in Frozen, whatever it is.
- Stock and broth go in Canned & Jarred, even when sold in cartons.
- An oil or spice the dish depends on, such as toasted sesame oil, smoked
  paprika or saffron, goes in its department, not Check pantry.
- Within a department, list items in the order a shopper would want them,
  or alphabetically if no order is obvious.

If the user has said how their store is laid out, use their order instead.

## 4. Check before printing

Show the grouped list in chat: department headings, then one `item — qty` per
line. Say which dishes it covers and list any ingredient you inferred. Then ask:
"Anything you already have, or anything to add?" Apply their changes. Skip this
step when they've said to just print it.

## 5. Print

Compose the job yourself from the printer's primitives (the `thermal` skill
lists them all). There is no grocery template in the binary, on purpose: the
layout lives here, so it can change without a new binary release. The layout:

- a `big` title (keep it short: "Groceries", the store name, "Sunday dinner"),
- a centred `text` line naming the dishes, shortened to one line if possible,
- a `rule` of `=`,
- for each department, a size-2 bold inverted `text` heading with a space on
  either side of the name, one `row` per item (`[ ] name` on the left, qty on
  the right, `.` fill), then a `space`.

```json
{ "blocks": [
  { "type": "big", "text": "Groceries", "height": 7 },
  { "type": "text", "text": "Tikka masala + naan", "align": "center" },
  { "type": "rule", "char": "=" },
  { "type": "text", "text": " Produce ", "size": 2, "bold": true, "invert": true },
  { "type": "row", "left": "[ ] Yellow onions", "right": "3", "fill": "." },
  { "type": "row", "left": "[ ] Garlic", "right": "1 head", "fill": "." },
  { "type": "space" },
  { "type": "text", "text": " Check pantry ", "size": 2, "bold": true, "invert": true },
  { "type": "row", "left": "[ ] Garam masala", "right": "have?", "fill": "." },
  { "type": "space" }
] }
```

- Write the job to a file in a temporary directory, then preview it with
  `print-to-thermal preview <file>`. Fix every `warning:` and `OVERFLOW`.
  Long names wrap onto a second line, which is fine. Shorten them only if the
  list becomes hard to read.
- Print with `print-to-thermal print <file>`. "sent" means the printer
  accepted the job, not that paper came out. Say so.
- Text prints as ASCII only. Accents and symbols such as `é` and `½` are
  converted automatically ("jalapeno", "1/2"), so write fractions as `1/2`.

A list longer than about 40 items makes a long receipt. Mention that, and
offer to split it into one print per store or per trip.
