#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2010-2011 Umeå University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#            http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Contains a class that can be used handle all the ECP handling for other python
programs.
"""

import cookielib
import getpass
import sys

from saml2 import soap
from saml2 import samlp
from saml2 import BINDING_PAOS

from saml2.profile import paos
from saml2.profile import ecp

from saml2.metadata import MetaData

SERVICE = "urn:oasis:names:tc:SAML:2.0:profiles:SSO:ecp"
PAOS_HEADER_INFO = 'ver="%s";"%s"' % (paos.NAMESPACE, SERVICE)

class Client(object):
    def __init__(self, user, passwd, sp="", idp=None, metadata_file=None,
                 xmlsec_binary=None, verbose=0):
        """
        :param user: user name
        :param passwd: user password
        :param sp: The SP URL
        :param idp: The IdP PAOS endpoint
        :param metadata_file: Where the metadata file is if used
        :param xmlsec_binary: Where the xmlsec1 binary can be found
        :param verbose: Chatty or not
        """
        self._idp = idp
        self._sp = sp
        self.user = user
        self.passwd = passwd
        if metadata_file:
            self._metadata = MetaData()
            self._metadata.import_metadata(open(metadata_file).read(),
                                           xmlsec_binary)
        else:
            self._metadata = None
        self._verbose = verbose
        self.cookie_handler = None

        self.done_ecp = False
        self.cookie_jar = cookielib.LWPCookieJar()
        self.http = soap.HTTPClient(self._sp, cookiejar=self.cookie_jar)

    def find_idp_endpoint(self, idp_entity_id):
        if self._idp:
            return self._idp

        if idp_entity_id and not self._metadata:
            raise Exception(
                "Can't handle IdP entity ID if I don't have metadata")

        if idp_entity_id:
            ssos = self._metadata.single_sign_on_services(idp_entity_id,
                                                          binding=BINDING_PAOS)
            try:
                self._idp = ssos[0]
                return self._idp
            except TypeError:
                raise Exception(
                    "No suitable endpoint found for entity id '%s'"\
                    % (idp_entity_id,))

        raise Exception("No IdP endpoint found")

    def phase2(self, authn_request, rc_url, idp_entity_id, headers=None):
        """
        Doing the second phase of the ECP conversation

        :param authn_request: The AuthenticationRequest
        :param rc_url: The assertion consumer service url
        :param idp_entity_id: The EntityID of the IdP
        :param headers: Possible extra headers
        :return: The response from the IdP
        """
        idp_request = soap.make_soap_enveloped_saml_thingy(authn_request)
        #idp_endpoint = authn_request.destination
        idp_endpoint = self.find_idp_endpoint(idp_entity_id)

        if self.user and self.passwd:
            self.http.add_credentials(self.user, self.passwd)

        # POST the request to the IdP
        response = self.http.post(idp_request, headers=headers,
                                  path=idp_endpoint)

        if response is None or response is False:
            raise Exception(
                "Request to IdP failed (%s): %s" % (self.http.response.status,
                                                    self.http.error_description))

        if self._verbose:
            print >> sys.stderr, "IdP response: %s" % response

        # SAMLP response in a SOAP envelope body, ecp response in headers
        respdict = soap.class_instances_from_soap_enveloped_saml_thingies(
                                                response, [paos, ecp,samlp])

        if respdict is None:
            raise Exception("Unexpected reply from the IdP")

        if self._verbose:
            print >> sys.stderr, "IdP reponse dict: %s" % respdict

        idp_response = respdict["body"]
        assert idp_response.c_tag == "Response"

        if self._verbose:
            print >> sys.stderr, "IdP AUTHN response: %s" % idp_response
            print

        _ecp_response = None
        for item in respdict["header"]:
            if item.c_tag == "Response" and\
               item.c_namespace == ecp.NAMESPACE:
                _ecp_response = item

        _acs_url = _ecp_response.assertion_consumer_service_url
        if rc_url != _acs_url:
            error = ("response_consumer_url '%s' does not match" % rc_url,
                     "assertion_consumer_service_url '%s" % _acs_url)
            # Send an error message to the SP
            fault_text = soap.soap_fault(error)
            _ = self.http.post(fault_text, path=rc_url)
            # Raise an exception so the user knows something went wrong
            raise Exception(error)
        
        return idp_response

    #noinspection PyUnusedLocal
    def ecp_conversation(self, respdict, idp_entity_id=None):
        """  """

        if respdict is None:
            raise Exception("Unexpected reply from the SP")

        if self._verbose:
            print >> sys.stderr, "SP reponse dict: %s" % respdict

        # AuthnRequest in the body or not
        authn_request = respdict["body"]
        assert authn_request.c_tag == "AuthnRequest"

        # ecp.RelayState among headers
        _relay_state = None
        _paos_request = None
        for item in respdict["header"]:
            if item.c_tag == "RelayState" and\
               item.c_namespace == ecp.NAMESPACE:
                _relay_state = item
            if item.c_tag == "Request" and\
               item.c_namespace == paos.NAMESPACE:
                _paos_request = item

        _rc_url = _paos_request.response_consumer_url

        # **********************
        # Phase 2 - talk to the IdP
        # **********************

        idp_response = self.phase2(authn_request, _rc_url, idp_entity_id)

        # **********************************
        # Phase 3 - back to the SP
        # **********************************

        sp_response = soap.make_soap_enveloped_saml_thingy(idp_response,
            [_relay_state])

        if self._verbose:
            print >> sys.stderr, sp_response
            print

        headers = {'Content-Type': 'application/vnd.paos+xml', }

        # POST the package from the IdP to the SP
        response = self.http.post(sp_response, headers, _rc_url)

        if not response:
            if self.http.response.status == 302:
                # ignore where the SP is redirecting us to and go for the
                # url I started off with.
                pass
            else:
                print self.http.error_description
                raise Exception(
                    "Error POSTing package to SP: %s" % self.http.response.reason)

        if self._verbose:
            print >> sys.stderr, "Final SP response: %s" % response

        self.done_ecp = True
        return None


    def operation(self, idp_entity_id, op, **opargs):
        if "path" not in opargs:
            opargs["path"] = self._sp

        # ********************************************
        # Phase 1 - First conversation with the SP
        # ********************************************
        # headers needed to indicate to the SP that I'm ECP enabled

        if "headers" in opargs and opargs["headers"]:
            opargs["headers"]["PAOS"] = PAOS_HEADER_INFO
            if "Accept" in opargs["headers"]:
                opargs["headers"]["Accept"] += ";application/vnd.paos+xml"
            elif "accept" in opargs["headers"]:
                opargs["headers"]["Accept"] = opargs["headers"]["accept"]
                opargs["headers"]["Accept"] += ";application/vnd.paos+xml"
                del opargs["headers"]["accept"]
        else:
            opargs["headers"] = {
                'Accept': 'text/html; application/vnd.paos+xml',
                'PAOS': PAOS_HEADER_INFO
            }

        # request target from SP
        # can remove the PAOS header now
#        try:
#            del opargs["headers"]["PAOS"]
#        except KeyError:
#            pass
        
        response = op(**opargs)
        if self._verbose:
            print >> sys.stderr, "SP response: %s" % response

        if not response:
            raise Exception(
                "Request to SP failed: %s" % self.http.error_description)

        # The response might be a AuthnRequest instance in a SOAP envelope
        # body. If so it's the start of the ECP conversation
        # Two SOAP header blocks; paos:Request and ecp:Request
        # may also contain a ecp:RelayState SOAP header block
        # If channel-binding was part of the PAOS header any number of
        # <cb:ChannelBindings> header blocks may also be present
        # if 'holder-of-key' option then one or more <ecp:SubjectConfirmation>
        # header blocks may also be present
        try:
            respdict = soap.class_instances_from_soap_enveloped_saml_thingies(
                response,
                [paos, ecp,
                 samlp])
            self.ecp_conversation(respdict, idp_entity_id)
            # should by now be authenticated so this should go smoothly
            response = op(**opargs)
        except (soap.XmlParseError, AssertionError, KeyError):
            pass

        #print "RESP",response, self.http.response

        if not response:
            if  self.http.response.status != 404:
                raise Exception("Error performing operation: %s" % (
                    self.http.error_description,))

        return response

    def delete(self, path=None, idp_entity_id=None):
        return self.operation(idp_entity_id, self.http.delete, path=path)

    def get(self, path=None, idp_entity_id=None, headers=None):
        return self.operation(idp_entity_id, self.http.get, path=path,
                              headers=headers)

    def post(self, path=None, data="", idp_entity_id=None, headers=None):
        return self.operation(idp_entity_id, self.http.post, data=data,
                              path=path, headers=headers)

    def put(self, path=None, data="", idp_entity_id=None, headers=None):
        return self.operation(idp_entity_id, self.http.put, data=data,
                              path=path, headers=headers)
