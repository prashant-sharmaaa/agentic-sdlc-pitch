
# Swag Labs (saucedemo.com) — E-Commerce Test Requirements

Base URL: https://www.saucedemo.com/
Credentials: username = standard_user, password = secret_sauce

## Authentication
- Users must log in with valid credentials before accessing any page
- After successful login, users are redirected to the product inventory page

## Product Catalog
- The inventory page must display product tiles with product name, image, and price
- Users can click a product name or image to open the product detail page
- The product detail page must show the product name, description, price, and an Add to cart button

## Shopping Cart
- Users can add a product to the cart from the inventory page using the Add to cart button
- After clicking Add to cart, the button label changes to Remove
- The cart badge in the top navigation updates to show the number of items in the cart
- Users can remove a product from the inventory page by clicking the Remove button
- After clicking Remove, the button label changes back to Add to cart

## Cart Page
- Users can navigate to the cart page at /cart.html
- The cart page lists all added items with their name, quantity, and price

## Sorting
- Users can sort products using the sort dropdown in the top right of the inventory page
- Sort options: Name (A to Z), Name (Z to A), Price (low to high), Price (high to low)
- Selecting Price (low to high) shows the cheapest product (Sauce Labs Onesie at $7.99) first
- Selecting Name (A to Z) shows Sauce Labs Backpack first
