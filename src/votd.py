import requests
from bs4 import BeautifulSoup

# specify the url
quote_page = 'https://www.verseoftheday.com/'
data = []

# query the website and return the html to the variable 'page'
page = requests.get(quote_page)

# parse the html using beautiful soup and store in variable 'soup'
soup = BeautifulSoup(page.text, 'html.parser')

# get the verse
name_box = soup.find('div', attrs={'class': 'bilingual-left'})
verse = name_box.text.strip() # strip() is used to remove starting and trailing

# remove the pesky unicode character -
x = verse.find(u'—')

# separate ref from verse
verse_ref = verse[x+1:]
verse = verse[:x]

# display verse
print(f"VERSE OF THE DAY\n{verse}\n{verse_ref}\n")
