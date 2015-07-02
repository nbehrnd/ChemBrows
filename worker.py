#!/usr/bin/python
# -*-coding:Utf-8 -*

import os
from PyQt4 import QtSql, QtCore
import feedparser
import functools
from requests_futures.sessions import FuturesSession
import requests
from io import open as iopen

import hosts
import functions



class Worker(QtCore.QThread):

    """Subclassing the class in order to provide a thread.
    The thread is used to parse the RSS flux, in background. The
    main UI remains functional"""

    # http://stackoverflow.com/questions/6783194/background-thread-with-qthread-in-pyqt
    # https://wiki.python.org/moin/PyQt/Threading,_Signals_and_Slots


    def __init__(self, logger, bdd, dict_journals, parent):

        QtCore.QThread.__init__(self)

        self.l = logger
        self.bdd = bdd
        self.dict_journals = dict_journals

        # Define a path attribute to easily change it
        # for the tests
        self.path = "./graphical_abstracts/"

        # Set the timeout for the futures
        # W/ a large timeout, less chances to get en exception
        self.TIMEOUT = 60

        # self.l.info("Starting parsing of the new articles")

        self.parent = parent

        self.count_futures_urls = 0
        self.count_futures_images = 0


    def setUrl(self, url_feed):

        self.url_feed = url_feed


    def __del__(self):

        """Method to destroy the thread properly"""

        self.l.debug("Deleting thread")

        # NE PAS décommenter
        # self.wait()

        self.exit()


    def run(self):

        """Main function. Starts the real business"""

        # Get the RSS page of the url provided
        try:
            self.feed = feedparser.parse(self.url_feed)
        except OSError:
            self.l.error("Too many files open, could not start the thread !")
            return

        # Get the journal name
        try:
            journal = self.feed['feed']['title']
        except KeyError:
            self.l.error("No title for the journal ! Aborting")
            self.l.error(self.url_feed)
            return

        self.l.info("{0}: {1}".format(journal, len(self.feed.entries)))

        # Lists to check if the post is in the db, and if
        # it has all the infos
        self.session_images = FuturesSession(max_workers=20)

        # Get the company and the journal_abb by scrolling the dictionnary
        # containing all the data regarding the journals implemented in the
        # program. This dictionnary is built in gui.py, to avoid multiple calls
        # to hosts.getJournals
        for key, tuple_data in self.dict_journals.items():
            if journal in tuple_data[0]:
                company = key
                index = tuple_data[0].index(journal)
                journal_abb = tuple_data[1][index]
                break

        try:
            self.list_doi, self.list_ok = self.listDoi(journal_abb)
        except UnboundLocalError:
            self.l.error("Journal not recognized ! Aborting")
            return

        # Create a list for the journals which a dl of the article
        # page is not required. All the data are in the rss page
        journals_no_dl = self.dict_journals['science'][0] + \
                         self.dict_journals['elsevier'][0] + \
                         self.dict_journals['beilstein'][0]

        query = QtSql.QSqlQuery(self.bdd)
        # self.bdd.transaction()

        # The feeds of these journals are complete
        # if journal in wiley + science + elsevier:
        if journal in journals_no_dl:

            self.count_futures_urls += len(self.feed.entries)

            for entry in self.feed.entries:

                # Get the DOI, a unique number for a publication
                doi = hosts.getDoi(company, journal, entry)

                if doi in self.list_doi and self.list_ok[self.list_doi.index(doi)]:
                    self.count_futures_images += 1
                    self.l.debug("Skipping")
                    continue
                else:
                    try:
                        title, date, authors, abstract, graphical_abstract, url, topic_simple = hosts.getData(company, journal, entry)
                    except TypeError:
                        self.l.error("getData returned None for {}".format(journal))
                        self.count_futures_images += 1
                        return

                    # Checking if the data are complete
                    # TODO: normally fot these journals, no need to check
                    if type(abstract) is not str:
                        verif = 0
                    else:
                        verif = 1

                    if doi in self.list_doi and doi not in self.list_ok:
                        query.prepare("UPDATE papers SET title=?, date=?, authors=?, abstract=?, verif=?, topic_simple=? WHERE doi=?")
                        params = (title, date, authors, abstract, verif, topic_simple, doi)
                        self.l.debug("Updating {0} in the database".format(doi))
                    else:
                        query.prepare("INSERT INTO papers(doi, title, date, journal, authors, abstract, graphical_abstract, url, verif, new, topic_simple)\
                                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
                        # Set new to 1 and not to true
                        params = (doi, title, date, journal_abb, authors, abstract, graphical_abstract, url, verif, 1, topic_simple)
                        self.l.debug("Adding {0} to the database".format(doi))
                        self.parent.counter += 1

                    for value in params:
                        query.addBindValue(value)

                    query.exec_()

                    if graphical_abstract == "Empty":
                        self.count_futures_images += 1
                    else:
                        # Use a user-agent browser, some journals block bots
                        # headers = {'User-agent': 'Mozilla/5.0',
                                   # 'Connection': 'close',
                                   # 'Referer': url}
                        headers = {'User-agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:12.0) Gecko/20100101 Firefox/21.0',
                                   'Connection': 'close',
                                   'Referer': url}

                        future_image = self.session_images.get(graphical_abstract, headers=headers, timeout=self.TIMEOUT)
                        future_image.add_done_callback(functools.partial(self.pictureDownloaded, doi, url))

        else:

            self.session_pages = FuturesSession(max_workers=20)

            for entry in self.feed.entries:

                doi = hosts.getDoi(company, journal, entry)

                if doi in self.list_doi and self.list_ok[self.list_doi.index(doi)]:
                    self.count_futures_images += 1
                    self.count_futures_urls += 1
                    self.l.debug("Skipping")
                    continue
                else:
                    try:
                        url = entry.feedburner_origlink
                    except AttributeError:
                        url = entry.link

                    headers = {'User-agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:12.0) Gecko/20100101 Firefox/21.0',
                               'Connection': 'close'}

                    future = self.session_pages.get(url, timeout=self.TIMEOUT, headers=headers)
                    future.add_done_callback(functools.partial(self.completeData, doi, company, journal, journal_abb, entry))

        while not self.checkFuturesRunning():
            self.sleep(0.5)

        # if not self.bdd.commit():
            # self.l.error(self.bdd.lastError().text())
            # self.l.error("Problem when comitting data for {}".format(journal))

        # Free the memory, and clean the remaining futures
        try:
            self.session_pages.executor.shutdown()
        except AttributeError:
            pass
        self.session_images.executor.shutdown()

        self.l.info("Exiting thread for {}".format(journal))


    def completeData(self, doi, company, journal, journal_abb, entry, future):

        """Callback to handle the response of the futures trying to
        download the page of the articles"""

        self.count_futures_urls += 1

        try:
            response = future.result()
        except requests.exceptions.ReadTimeout:
            self.l.error("ReadTimeout for {}".format(journal))
            self.count_futures_images += 1
            return
        except requests.exceptions.ConnectionError:
            self.l.error("ConnectionError for {}".format(journal))
            self.count_futures_images += 1
            return

        query = QtSql.QSqlQuery(self.bdd)

        try:
            title, date, authors, abstract, graphical_abstract, url, topic_simple = hosts.getData(company, journal, entry, response)
        except TypeError:
            self.l.error("getData returned None for {}".format(journal))
            self.count_futures_images += 1
            return

        # Checking if the data are complete
        if type(abstract) is not str or type(authors) is not str:
            verif = 0
        else:
            verif = 1

        if doi in self.list_doi and doi not in self.list_ok:
            query.prepare("UPDATE papers SET title=?, date=?, authors=?, abstract=?, verif=?, topic_simple=? WHERE doi=?")
            params = (title, date, authors, abstract, verif, topic_simple, doi)
            self.l.debug("Updating {0} in the database".format(doi))
        else:
            query.prepare("INSERT INTO papers(doi, title, date, journal, authors, abstract, graphical_abstract, url, verif, new, topic_simple)\
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")

            if graphical_abstract != "Empty":
                path_picture = functions.simpleChar(graphical_abstract)
            else:
                path_picture = "Empty"

            params = (doi, title, date, journal_abb, authors, abstract, path_picture, url, verif, 1, topic_simple)
            self.l.debug("Adding {0} to the database".format(doi))
            self.parent.counter += 1

        for value in params:
            query.addBindValue(value)

        query.exec_()

        if graphical_abstract == "Empty" or os.path.exists(self.path + functions.simpleChar(graphical_abstract)):
            self.count_futures_images += 1
        else:
            headers = {'User-agent': 'Mozilla/5.0',
                       'Connection': 'close',
                       'Referer': url}

            headers = {'User-agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:12.0) Gecko/20100101 Firefox/21.0',
                       'Connection': 'close',
                       'Referer': url}

            future_image = self.session_images.get(graphical_abstract, headers=headers, timeout=self.TIMEOUT)
            future_image.add_done_callback(functools.partial(self.pictureDownloaded, doi, url))


    def pictureDownloaded(self, doi, entry_url, future):

        """Callback to handle the response of the futures
        downloading a picture"""

        self.count_futures_images += 1

        query = QtSql.QSqlQuery(self.bdd)

        try:
            response = future.result()
        except requests.exceptions.ReadTimeout:
            self.l.error("ReadTimeout for image: {}".format(entry_url))
            params = ("Empty", 0, doi)
        except requests.exceptions.ConnectionError:
            self.l.error("ConnectionError for image: {}".format(entry_url))
            params = ("Empty", 0, doi)
        except requests.exceptions.MissingSchema:
            self.l.error("MissingSchema for image: {}".format(entry_url))
            params = ("Empty", 0, doi)
        else:
            if response.status_code is requests.codes.ok:

                path = self.path

                try:
                    # Save the page
                    with iopen(path + functions.simpleChar(response.url), 'wb') as file:
                        file.write(response.content)
                        self.l.debug("Image ok")
                except OSError:
                    params = ("Empty", 0, doi)
                else:
                    # graphical_abstract = functions.simpleChar(response.url)
                    # params = (graphical_abstract, 1, doi)
                    params = (1, doi)
            else:
                self.l.debug("Bad return code: {}".format(response.status_code))
                params = ("Empty", 0, doi)

        finally:
            query.prepare("UPDATE papers SET verif=? WHERE doi=?")

            for value in params:
                query.addBindValue(value)

            query.exec_()


    def checkFuturesRunning(self):

        """Method to check if some futures are still running.
        Returns True if all the futures are done"""

        if self.count_futures_images + self.count_futures_urls != len(self.feed.entries) * 2:
            return False
        else:
            return True


    def listDoi(self, journal_abb):

        """Function to get the doi from the database.
        Also returns a list of booleans to check if the data are complete"""

        list_doi = []
        list_ok = []

        query = QtSql.QSqlQuery(self.bdd)
        query.prepare("SELECT * FROM papers WHERE journal=?")
        query.addBindValue(journal_abb)
        query.exec_()

        while query.next():
            record = query.record()
            list_doi.append(record.value('doi'))

            if record.value('verif') == 1 and record.value('graphical_abstract') != "Empty":
                # Try to download the images again if it didn't work before
                list_ok.append(True)
            else:
                list_ok.append(False)

        return list_doi, list_ok
